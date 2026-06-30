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
    ) -> str:
        """Manually start a Langfuse trace.

        Call this before invoking the agent when the callback-based
        ``on_chain_start`` does not fire (e.g. inside a LangGraph node).
        """
        self._trace_id = str(uuid.uuid4())
        self._trace_name = name or self._trace_name
        self._client.trace(
            id=self._trace_id,
            name=self._trace_name,
            session_id=self._session_id,
            input=input_data,
            tags=self._tags,
        )
        logger.debug(
            "langfuse_trace_created",
            extra={"trace_id": self._trace_id, "session_id": self._session_id},
        )
        return self._trace_id

    def end_trace(
        self,
        output_data: dict[str, Any] | None = None,
    ) -> None:
        """Manually end the current Langfuse trace and flush data.

        Call this after the agent completes.
        """
        if self._trace_id is None:
            return

        self._client.trace(
            id=self._trace_id,
            output=output_data,
        )
        self._client.flush()
        logger.debug("langfuse_trace_ended", extra={"trace_id": self._trace_id})

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
        """Create a Langfuse trace when the agent chain starts."""
        if parent_run_id is not None:
            return  # Only create trace at top-level chain

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
        """Record start of an LLM call."""
        self._llm_start_ts = time.monotonic()
        self._llm_call_idx += 1

        model_name = kwargs.get("invocation_params", {}).get(
            "model_name",
            serialized.get("kwargs", {}).get("model_name", "unknown"),
        )

        self._llm_input = {
            "messages": [
                {"role": self._msg_role(m), "content": str(m.content)[:2000]}
                for m in kwargs.get("invocation_params", {}).get("messages", [])
            ],
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

        if self._trace_id:
            self._client.generation(
                trace_id=self._trace_id,
                name=f"llm_call_{self._llm_call_idx}",
                model=model_name,
                input=self._llm_input,
                output={"content": output_text[:2000]},
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

    # ── Tool call tracking ──────────────────────────────────────

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
        """Record start of a tool invocation."""
        self._tool_call_idx += 1
        self._tool_start_ts = time.monotonic()
        self._tool_name = serialized.get("name", "unknown_tool")

        logger.debug(
            "langfuse_tool_start",
            extra={
                "trace_id": self._trace_id,
                "tool": self._tool_name,
                "call_idx": self._tool_call_idx,
            },
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record end of a tool invocation."""
        latency_ms = (time.monotonic() - getattr(self, "_tool_start_ts", 0.0)) * 1000

        if self._trace_id:
            self._client.span(
                trace_id=self._trace_id,
                name=f"tool_{self._tool_name}_{self._tool_call_idx}",
                input={"args": self._last_tool_input or {}},
                output={"result": str(output)[:2000]},
                metadata={
                    "latency_ms": round(latency_ms, 1),
                    "tool_name": self._tool_name,
                    "run_id": str(run_id),
                },
            )

        logger.debug(
            "langfuse_tool_end",
            extra={
                "trace_id": self._trace_id,
                "tool": self._tool_name,
                "latency_ms": round(latency_ms, 1),
            },
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record tool invocation error."""
        if self._trace_id:
            self._client.span(
                trace_id=self._trace_id,
                name=f"tool_{self._tool_name}_{self._tool_call_idx}",
                metadata={
                    "error": str(error),
                    "tool_name": self._tool_name,
                    "run_id": str(run_id),
                },
            )

    # ── Agent action (tool call decisions) ──────────────────────

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Capture the tool arguments before execution."""
        self._last_tool_input = {
            "tool": getattr(action, "tool", "unknown"),
            "tool_input": str(getattr(action, "tool_input", "")),
        }

    # ── Helpers ─────────────────────────────────────────────────

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
