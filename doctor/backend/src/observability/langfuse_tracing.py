"""
Langfuse tracing integration for Doctor Agent LLM observability.

Provides a ``get_langfuse_handler()`` factory that returns a LangChain-compatible
``CallbackHandler``. When passed to ``agent.ainvoke(config={"callbacks": [...]})``,
it automatically captures:

- LLM call input/output/token/cost/model
- Tool call name/args/result

Uses the base ``langfuse`` Python SDK directly (NOT langfuse-langchain, which is
incompatible with langchain >= 1.0). The callback handler implements
``BaseCallbackHandler`` from ``langchain_core.callbacks``.

Usage::

    from src.observability.langfuse_tracing import get_langfuse_handler

    langfuse_handler = get_langfuse_handler()
    result = await agent.ainvoke(
        {"messages": [...]},
        config={"callbacks": [langfuse_handler]},
    )

Note:
    OTel (``observability/__init__.py``) remains unchanged and handles HTTP
    request-level tracing. Langfuse handles LLM-level observability.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langfuse import Langfuse

from src.config import settings

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════
# LangChain Callback Handler for Langfuse
# ═════════════════════════════════════════════════════════════════════


class LangfuseCallbackHandler(BaseCallbackHandler):
    """
    LangChain callback that traces LLM calls and tool invocations to Langfuse.

    Creates a Langfuse trace per diagnosis session and nests each LLM call
    as a "generation" observation. Tool calls are recorded as "span" observations.
    """

    def __init__(
        self,
        *,
        secret_key: str,
        public_key: str,
        host: str,
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._client = Langfuse(
            secret_key=secret_key,
            public_key=public_key,
            host=host,
        )
        self._session_id = session_id or str(uuid.uuid4())
        self._tags = tags or []
        self._trace_id: str | None = None
        self._trace_name: str = "doctor-diagnosis"
        self._llm_call_idx: int = 0
        self._tool_call_idx: int = 0

        # Per-LLM-call timing
        self._llm_start_ts: float = 0.0
        self._llm_input: dict[str, Any] | None = None
        self._current_llm_run_id: uuid.UUID | None = None  # 去重：防 on_chat_model_start + on_llm_start 双 fire

        # Per-tool-call tracking
        self._tool_name: str = "unknown_tool"
        self._tool_start_ts: float = 0.0
        self._last_tool_input: dict[str, Any] | None = None

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    # ── Manual trace lifecycle (for LangGraph contexts where
    #    on_chain_start/on_chain_end don't fire) ─────────────────

    def start_trace(
        self,
        name: str | None = None,
        input_data: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> str:
        """Manually start a Langfuse trace.

        Call this before invoking the agent when the callback-based
        ``on_chain_start`` does not fire (e.g. inside a LangGraph node).

        Args:
            name: Optional trace name (defaults to ``self._trace_name``).
            input_data: Optional trace input payload.
            trace_id: Optional external trace ID to reuse. When provided
                (e.g. by the Experiment runner), all observations recorded
                by this handler land on that trace — enabling process-quality
                scorers to read them on the same trace that is being scored.
                When omitted, a new UUID is generated.
        """
        self._trace_id = trace_id if trace_id else str(uuid.uuid4())
        if name:
            self._trace_name = name
        # When reusing an external trace_id without an explicit name,
        # keep self._trace_name untouched and upsert WITHOUT name so the
        # original creator's name (e.g. "baseline_phase0_BE-020") is
        # preserved — Langfuse upsert with name=None keeps the existing name.
        # Reset per-trace counters so tool/llm indices are scoped to this trace
        self._llm_call_idx = 0
        self._tool_call_idx = 0
        if trace_id and not name:
            # Reusing an existing trace created by an external caller (e.g. the
            # Experiment runner). Upsert with id (+input) ONLY — do NOT pass
            # session_id/tags/name/metadata, otherwise Langfuse upsert would
            # overwrite the original creator's session_id (the runner groups
            # traces by session_id=run_name; overriding it breaks Sessions view).
            self._client.trace(
                id=self._trace_id,
                input=input_data,
            )
        else:
            self._client.trace(
                id=self._trace_id,
                name=self._trace_name,
                session_id=self._session_id,
                input=input_data,
                tags=self._tags,
            )
        logger.debug(
            "langfuse_trace_created",
            extra={
                "trace_id": self._trace_id,
                "session_id": self._session_id,
                "reused": trace_id is not None,
            },
        )
        return self._trace_id

    def end_trace(
        self,
        output_data: dict[str, Any] | None = None,
    ) -> None:
        """Manually end the current Langfuse trace and flush data.

        Call this after the agent completes. Resets ``_trace_id`` so the
        handler is stateless between cases——避免下一次 ``on_chain_start``
        复用上一次的 trace_id（在外部 caller 忘了调 ``start_trace`` 的退化
        场景下尤其重要）。
        """
        if self._trace_id is None:
            return

        self._client.trace(
            id=self._trace_id,
            output=output_data,
        )
        self._client.flush()
        logger.debug("langfuse_trace_ended", extra={"trace_id": self._trace_id})
        self._trace_id = None
        self._llm_call_idx = 0
        self._tool_call_idx = 0
        self._current_llm_run_id = None
        self._llm_input = None

    # ── Manual observation helpers (for manual agent loops where
    #    tool callbacks don't fire) ────────────────────────────────

    def record_tool_span(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any] | str,
        result: str,
        latency_ms: float,
        iteration: int,
        error: str | None = None,
    ) -> None:
        """Record a single tool call as a Langfuse SPAN observation.

        Used inside the manual agent loop where tools are invoked directly
        (``await tool.ainvoke(args)`` without a callback config), so the
        ``on_tool_start`` / ``on_tool_end`` callbacks never fire. Calling
        this explicitly ensures every tool invocation is captured on the
        trace for process-quality scoring.

        Args:
            tool_name: Name of the tool (e.g. ``search_observability``).
            tool_args: Arguments passed to the tool (dict or stringified).
            result: The (already-truncated) tool result that entered the
                agent context. Truncated again here to 2000 chars for the
                Langfuse UI display limit.
            latency_ms: Wall-clock latency of the tool call in ms.
            iteration: 1-based agent loop iteration index.
            error: Optional error string if the tool raised.
        """
        if self._trace_id is None:
            return

        self._tool_call_idx += 1
        metadata: dict[str, Any] = {
            "tool_name": tool_name,
            "latency_ms": round(latency_ms, 1),
            "iteration": iteration,
            "run_id": None,
        }
        if error is not None:
            metadata["error"] = error

        self._client.span(
            trace_id=self._trace_id,
            name=f"tool_{tool_name}_{self._tool_call_idx}",
            input={"args": tool_args},
            output={"result": result[:20000]},
            metadata=metadata,
        )

    def record_tool_skipped(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any] | str,
        iteration: int,
    ) -> None:
        """Record a deduplicated (skipped) tool call as a lightweight EVENT.

        Captures the agent's tendency to repeat identical tool calls, which
        process-quality scoring uses to compute the dedup ratio. Recorded as
        an EVENT (not SPAN) so it does not inflate the real tool-call count.
        """
        if self._trace_id is None:
            return

        self._client.event(
            trace_id=self._trace_id,
            name=f"tool_skipped_{tool_name}",
            input={"args": tool_args},
            metadata={
                "tool_name": tool_name,
                "iteration": iteration,
                "deduplicated": True,
            },
        )

    def record_structured_output(
        self,
        *,
        schema_name: str,
        parsed: dict[str, Any] | None,
        raw_content: str | None = None,
        raw_tool_calls: list[dict[str, Any]] | None = None,
        error: str | None = None,
        case_id: str | None = None,
    ) -> None:
        """Record a ``with_structured_output`` result as a Langfuse SPAN.

        ``with_structured_output``'s parsed Pydantic object is invisible to
        the callback path: the callback fires on the raw model response
        (an AIMessage with ``content="" + tool_calls=[{schema, args}]`` for
        ``method="function_calling"``), and LangChain materializes the
        parsed Pydantic object from the tool_call args AFTER the callback
        fires. So ``on_llm_end`` can capture the raw tool_call (via
        ``_extract_tool_calls``) but never the parsed result.

        This method lets the forced-call wrapper explicitly record the
        parsed structured output + the JSON-serialized form that actually
        flows into the final report — making the Iteration 2 forced call
        mechanism visible end-to-end in Langfuse.

        Args:
            schema_name: Name of the structured output schema (e.g.
                ``"ForcedDiagnosisReport"``). Used as the span name suffix.
            parsed: The parsed object as a dict (e.g.
                ``pydantic_model.model_dump()``), or None if parsing failed.
            raw_content: Optional raw ``content`` string of the model
                response (usually "" for function_calling method).
            raw_tool_calls: Optional raw ``tool_calls`` list from the model
                response — the unparsed precursor of ``parsed``.
            error: Optional error string if the structured-output call
                failed (API error / model emitted no matching tool_call).
            case_id: Optional case_id for metadata tagging.
        """
        if self._trace_id is None:
            return

        output: dict[str, Any] = {}
        if parsed is not None:
            output["parsed"] = parsed
        if raw_content is not None:
            output["raw_content"] = raw_content[:20000]
        if raw_tool_calls is not None:
            output["raw_tool_calls"] = raw_tool_calls

        metadata: dict[str, Any] = {"schema_name": schema_name}
        if error is not None:
            metadata["error"] = error
        if case_id is not None:
            metadata["case_id"] = case_id

        self._client.span(
            trace_id=self._trace_id,
            name=f"structured_output_{schema_name}",
            input=None,
            output=output or None,
            metadata=metadata,
        )

    # ── Trace lifecycle (callback-based) ────────────────────────

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a Langfuse trace when the agent chain starts.

        若 ``start_trace(trace_id=...)`` 已被外部 caller 调用（实验 runner 复用
        trace_id 模式），此处**不要**创建新 trace——否则会覆盖外部 caller 设的
        trace_id，导致后续 LLM/tool observation 全部落到孤儿 trace 上，被评分
        的目标 trace 反而空着。早期版本因这里无条件 ``str(uuid.uuid4())`` 触发过
        大量 0-observation 孤儿 trace（session 视图里同名 trace 重复 9+ 次）。
        """
        if parent_run_id is not None:
            return  # Only create trace at top-level chain
        if self._trace_id is not None:
            # 外部 caller 已通过 start_trace 设了 trace_id，复用之，不创建新 trace
            logger.debug(
                "langfuse_chain_start_reused_trace",
                extra={"trace_id": self._trace_id, "session_id": self._session_id},
            )
            return

        self._trace_id = str(uuid.uuid4())
        self._client.trace(
            id=self._trace_id,
            name=self._trace_name,
            session_id=self._session_id,
            input=inputs,
            tags=self._tags,
        )
        logger.debug(
            "langfuse_trace_created",
            extra={"trace_id": self._trace_id, "session_id": self._session_id},
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Update the Langfuse trace with final output."""
        if parent_run_id is not None or self._trace_id is None:
            return

        self._client.trace(
            id=self._trace_id,
            output=outputs,
        )
        # Flush to ensure data is sent
        self._client.flush()
        logger.debug("langfuse_trace_ended", extra={"trace_id": self._trace_id})

    # ── LLM call tracking ───────────────────────────────────────

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record start of a chat model call.

        Chat 模型（ChatDeepSeek/ChatOpenAI）触发的是 on_chat_model_start 而非
        on_llm_start，且 messages 作为位置参数（List[List[BaseMessage]]）传入——
        不在 invocation_params 里。这是修复 generation.input 为空 ``{"messages": []}``
        盲区的关键回调。
        """
        if run_id == self._current_llm_run_id:
            return  # on_llm_start 已处理过同一 run，跳过防双计数
        self._current_llm_run_id = run_id
        self._llm_start_ts = time.monotonic()
        self._llm_call_idx += 1

        # messages 是 List[List[BaseMessage]]，取第一个 batch（通常只有一个）
        msg_list = messages[0] if messages else []
        self._llm_input = {
            "messages": [self._serialize_message(m) for m in msg_list],
        }

        logger.debug(
            "langfuse_chat_model_start",
            extra={
                "trace_id": self._trace_id,
                "run_id": str(run_id),
                "call_idx": self._llm_call_idx,
                "msg_count": len(msg_list),
            },
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record start of an LLM call.

        对 chat 模型通常不 fire（on_chat_model_start 负责）；对 completion-style
        LLM fire。若 on_chat_model_start 已处理同一 run_id 则跳过防双计数。
        """
        if run_id == self._current_llm_run_id:
            return  # on_chat_model_start 已处理
        self._current_llm_run_id = run_id
        self._llm_start_ts = time.monotonic()
        self._llm_call_idx += 1

        model_name = kwargs.get("invocation_params", {}).get(
            "model_name",
            serialized.get("kwargs", {}).get("model_name", "unknown"),
        )

        # completion-style LLM：用 prompts（stringified messages）兜底
        self._llm_input = {
            "messages": [
                {"role": "user", "content": p[:2000]} for p in (prompts or [])
            ],
            "model": model_name,
        }

        logger.debug(
            "langfuse_llm_start",
            extra={
                "trace_id": self._trace_id,
                "run_id": str(run_id),
                "model": model_name,
                "call_idx": self._llm_call_idx,
            },
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record end of an LLM call, flush to Langfuse."""
        latency_ms = (time.monotonic() - self._llm_start_ts) * 1000
        model_name = self._extract_model_name(response)

        # Extract token usage from response
        usage = self._extract_usage(response)
        output_text = self._extract_output_text(response)
        # Extract tool_calls from the response AIMessage (if any). Without this,
        # any tool-call AIMessage (ReAct loop's tool decisions AND the Iteration 2
        # forced final JSON call's with_structured_output emission) shows up in
        # Langfuse as a 0-char content generation — the model's actual
        # tool_call decision (which tool, which args) is invisible.
        tool_calls = self._extract_tool_calls(response)

        output: dict[str, Any] = {"content": output_text[:20000]}
        if tool_calls:
            output["tool_calls"] = tool_calls

        if self._trace_id:
            self._client.generation(
                trace_id=self._trace_id,
                name=f"llm_call_{self._llm_call_idx}",
                model=model_name,
                input=self._llm_input,
                output=output,
                usage=usage,
                usage_details=usage,
                metadata={
                    "latency_ms": round(latency_ms, 1),
                    "run_id": str(run_id),
                },
            )

        logger.debug(
            "langfuse_llm_end",
            extra={
                "trace_id": self._trace_id,
                "run_id": str(run_id),
                "model": model_name,
                "latency_ms": round(latency_ms, 1),
                "tokens": usage.get("input", 0) + usage.get("output", 0),
            },
        )
        self._llm_input = None
        self._current_llm_run_id = None  # 释放，允许下一轮 LLM 调用被记录

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record LLM call error."""
        if self._trace_id:
            self._client.generation(
                trace_id=self._trace_id,
                name=f"llm_call_{self._llm_call_idx}",
                model="unknown",
                input=self._llm_input,
                output=None,
                metadata={
                    "error": str(error),
                    "run_id": str(run_id),
                },
            )
        logger.warning(
            "langfuse_llm_error",
            extra={"trace_id": self._trace_id, "error": str(error)},
        )
        self._llm_input = None
        self._current_llm_run_id = None

    # ── Tool call tracking ──────────────────────────────────────

    # ── Tool callback hooks — INTENTIONALLY NO-OP ───────────────
    #
    # Tool observations are recorded by a SINGLE source of truth:
    # ``record_tool_span`` (and ``record_tool_skipped``), called explicitly by
    # ``LangfuseTracingMiddleware.awrap_tool_call`` / ``ToolDedupMiddleware``.
    #
    # Why these callbacks are no-ops:
    #   In the hand-written ReAct loop, tools were invoked with NO callback
    #   config (``await tool.ainvoke(args)``), so ``on_tool_start`` / ``on_tool_end``
    #   never fired — ``record_tool_span`` was already the sole recorder there.
    #   In the ``create_agent`` framework, the Langfuse handler is attached to the
    #   model via ``awrap_model_call`` (``model.with_config({"callbacks": [h]})``).
    #   LangGraph's Runnable-config contextvar propagates that callback config to
    #   the ToolNode too, so ``on_tool_start`` / ``on_tool_end`` WOULD fire here
    #   and double-record every tool call (callback span + ``record_tool_span``),
    #   which tanked ``process_quality`` (framework-smoke 0.806 vs baseline 0.958).
    #   Neutering the callback path makes ``record_tool_span`` the single source
    #   in BOTH implementations, so observability is identical and the
    #   ``_tool_call_idx`` counter is owned solely by ``record_tool_span``.
    #
    # Baseline safety: these never fired in the hand-written loop (no callback
    # config on tools), so no-op'ing them does not change baseline traces.

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
        ) -> None:
        """No-op — see class-level note on tool callback hooks."""
        return

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """No-op — see class-level note on tool callback hooks."""
        return

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """No-op — see class-level note on tool callback hooks."""
        return

    # ── Agent action (tool call decisions) ──────────────────────

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """No-op — tool args are captured by ``record_tool_span`` via the
        middleware ``awrap_tool_call`` path, not via this callback. See the
        class-level note on tool callback hooks for why callback-based tool
        recording is disabled."""
        return

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _serialize_message(msg: BaseMessage) -> dict[str, Any]:
        """把一条 LangChain message 序列化成可入 Langfuse 的 dict。

        除 role + content 外，还捕获 AIMessage 的 tool_calls（agent 决定调什么工具）
        和 ToolMessage 的 tool_call_id（对应哪次工具调用）——这两者在调试 agent
        行为时关键，缺了就看不到「LLM 这轮决定了什么工具调用」。
        """
        type_name = type(msg).__name__
        role_map = {
            "SystemMessage": "system",
            "HumanMessage": "user",
            "AIMessage": "assistant",
            "ToolMessage": "tool",
            "FunctionMessage": "function",
        }
        entry: dict[str, Any] = {
            "role": role_map.get(type_name, "unknown"),
            # 20000 安全网：truncate_tool_result 已在上游把 tool 结果压到 ≤8000，
            # 系统提示也就几千字——这个上限远超真实消息长度，只为防病态长输入。
            # 之前用 2000 会切掉 tool 结果的实际证据（agent 据此诊断的内容）+
            # 系统提示末尾的动态 phase 策略（CONVERGING/FINALIZING 文本）。
            "content": str(msg.content)[:20000],
        }
        # AIMessage 的 tool_calls：agent 这轮决定调哪些工具
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [
                {"name": tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""),
                 "args": tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})}
                for tc in tool_calls
            ]
        # ToolMessage 的 tool_call_id：这条结果对应哪次工具调用
        tcid = getattr(msg, "tool_call_id", None)
        if tcid:
            entry["tool_call_id"] = tcid
        return entry

    @staticmethod
    def _msg_role(msg: BaseMessage) -> str:
        """Map LangChain message type to OpenAI-style role."""
        type_name = type(msg).__name__
        role_map = {
            "SystemMessage": "system",
            "HumanMessage": "user",
            "AIMessage": "assistant",
            "ToolMessage": "tool",
            "FunctionMessage": "function",
        }
        return role_map.get(type_name, "unknown")

    @staticmethod
    def _extract_model_name(response: LLMResult) -> str:
        """Extract model name from LLMResult."""
        if response.llm_output and "model_name" in response.llm_output:
            return str(response.llm_output["model_name"])
        for gen in response.generations:
            if gen and hasattr(gen[0], "message") and hasattr(gen[0].message, "response_metadata"):
                meta = gen[0].message.response_metadata
                if "model_name" in meta:
                    return str(meta["model_name"])
        return "unknown"

    @staticmethod
    def _extract_usage(response: LLMResult) -> dict[str, int]:
        """Extract token usage from LLMResult."""
        usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        if response.llm_output and "token_usage" in response.llm_output:
            tu = response.llm_output["token_usage"]
            usage["input"] = tu.get("prompt_tokens", 0)
            usage["output"] = tu.get("completion_tokens", 0)
            usage["total"] = tu.get("total_tokens", 0)
        # Fallback: sum from generations
        for gen in response.generations:
            if gen and hasattr(gen[0], "message") and hasattr(gen[0].message, "usage_metadata"):
                um = gen[0].message.usage_metadata
                usage["input"] += um.get("input_tokens", 0)
                usage["output"] += um.get("output_tokens", 0)
                usage["total"] += um.get("total_tokens", 0)
        return usage

    @staticmethod
    def _extract_output_text(response: LLMResult) -> str:
        """Extract output text from LLMResult."""
        texts: list[str] = []
        for gen in response.generations:
            for g in gen:
                if hasattr(g, "message") and hasattr(g.message, "content"):
                    texts.append(str(g.message.content))
                elif hasattr(g, "text"):
                    texts.append(str(g.text))
        return "\n".join(texts)

    @staticmethod
    def _extract_tool_calls(response: LLMResult) -> list[dict[str, Any]]:
        """Extract tool_calls from the first AIMessage in the response.

        Captures the LLM's tool-call decisions (which tool, which args) so
        they're visible in the Langfuse generation output. Without this,
        any tool-call AIMessage shows up as ``{"content": ""}`` — the
        model's actual decision is invisible. Critical for two paths:

        1. ReAct loop iterations where the agent decides which tool to
           call next (content="" + tool_calls=[{search_observability, ...}]).
           Without this, the trace shows empty content with no link to the
           subsequent tool span.
        2. Iteration 2 forced final JSON call via
           ``with_structured_output(method="function_calling")``: the model
           emits an AIMessage with content="" + tool_calls=[{ForcedDiagnosisReport,
           args={...}}]. Without this, the forced call looks like a 0-char
           output — no evidence of what was produced. (The parsed Pydantic
           object is recorded separately via ``record_structured_output``
           because LangChain materializes it AFTER this callback fires.)
        """
        calls: list[dict[str, Any]] = []
        for gen in response.generations:
            for g in gen:
                msg = getattr(g, "message", None)
                if msg is None:
                    continue
                tcs = getattr(msg, "tool_calls", None) or []
                for tc in tcs:
                    if isinstance(tc, dict):
                        calls.append(
                            {
                                "name": tc.get("name", "?"),
                                "args": tc.get("args", {}),
                                "id": tc.get("id", ""),
                            }
                        )
        return calls


# ═════════════════════════════════════════════════════════════════════
# Factory function
# ═════════════════════════════════════════════════════════════════════

_langfuse_handler: LangfuseCallbackHandler | None = None


def get_langfuse_handler(
    session_id: str | None = None,
    tags: list[str] | None = None,
) -> LangfuseCallbackHandler:
    """
    Get or create a cached Langfuse CallbackHandler.

    The handler is created once and reused across all diagnosis sessions
    for the lifetime of the process. Uses settings from ``config.Settings``.

    Args:
        session_id: Optional session ID for grouping traces.
        tags: Optional tags to attach to each trace.

    Returns:
        A LangChain-compatible CallbackHandler for Langfuse tracing.

    Raises:
        ValueError: If ``langfuse_secret_key`` or ``langfuse_public_key``
            is not configured.
    """
    global _langfuse_handler

    if _langfuse_handler is not None:
        return _langfuse_handler

    secret_key = settings.langfuse_secret_key
    public_key = settings.langfuse_public_key

    if not secret_key or not public_key:
        raise ValueError(
            "Langfuse credentials not configured. "
            "Set LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY environment variables."
        )

    _langfuse_handler = LangfuseCallbackHandler(
        secret_key=secret_key,
        public_key=public_key,
        host=settings.langfuse_host,
        session_id=session_id,
        tags=tags,
    )

    logger.info(
        "langfuse_handler_created",
        extra={"host": settings.langfuse_host},
    )

    return _langfuse_handler


def clear_langfuse_handler_cache() -> None:
    """Clear the cached handler (useful for testing or credential rotation)."""
    global _langfuse_handler
    _langfuse_handler = None
    logger.info("langfuse_handler_cache_cleared")
