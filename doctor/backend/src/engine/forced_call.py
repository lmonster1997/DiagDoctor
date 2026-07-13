"""Forced final JSON call mechanism (Iteration 1 + Iteration 2 structured output).

Baseline (Iteration 0) showed two failure modes accounting for all disaster
cases:
  mode 1 (3/4 disaster): loop hits MAX_TOOL_CALLS cap, last AIMessage has
    content="" + tool_calls=[...] → parse_diagnosis_report returns a
    low-confidence fallback with empty root_cause.
  mode 2 (1/4 disaster + 2 regression cases): agent natural-stops but
    emits narrative prose without any JSON structure → same fallback.

Iteration 1 mechanism: after the ReAct loop ends, if the last AIMessage
does not contain extractable JSON, make ONE extra LLM call WITHOUT the
diagnostic tools bound (this is the v1 REPORTING-phase failure mode —
DeepSeek keeps emitting DSML tool-call markers when those tools are bound).

Iteration 2 change (this version): the forced call now uses
``llm.with_structured_output(ForcedDiagnosisReport)`` so the report schema
is enforced at the API level via tool calling, instead of asking the LLM
to emit free-text JSON and parsing it. This eliminates the unescaped-quote
/ trailing-comma parse failures observed in 3 disaster cases after
Iteration 1 (CONFIG-020 / DATA-020 / LOGIC-022).

Why this avoids the v1 DSML trap: ``with_structured_output`` binds exactly
ONE tool — the report schema — to a fresh LLM instance. The diagnostic
tools (code_search / get_file_content / etc.) are NOT bound, so DeepSeek
has no surface to fall back to emitting those tool_calls. The model emits
exactly one tool_call for the report schema; LangChain parses its args
into a ``ForcedDiagnosisReport`` Pydantic object.

The parsed Pydantic object is then serialized back to a JSON string and
wrapped in a synthetic ``AIMessage`` so the downstream
``parse_diagnosis_report`` (which expects ``content`` to be a JSON string)
picks it up unchanged — no other code path needs to know about the
structured-output swap.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from src.engine.budget.constants import MAX_TIME_SECONDS
from src.engine.parsing import _extract_json_from_text
from src.observability.logger import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════
# ForcedDiagnosisReport — the schema enforced via with_structured_output.
# Slimmer than DiagnosisReport: excludes early_stopped / notes (those are
# harness-controlled, not LLM-controlled) so the model can't fill them
# with junk. Field descriptions double as field-meaning guidance for the
# LLM, complementing the prompt instruction.
# ═════════════════════════════════════════════════════════════════════


class ForcedDiagnosisReport(BaseModel):
    """Schema for the forced final JSON call — only LLM-controllable fields.

    Enforced at the API level via ``with_structured_output`` (tool calling).
    The LLM emits a single tool_call whose args match this schema; LangChain
    parses it into this Pydantic object. No free-text JSON parsing involved
    on the happy path → no unescaped-quote / trailing-comma failures.
    """

    primary_category: str = Field(
        description=(
            "最高置信度的根因类别，从 frontend_crash / backend_error / "
            "performance / logic / data / config 中选一个"
        )
    )
    categories: list[str] = Field(
        default_factory=list,
        description="完整的多标签类别集合（通常 1-3 个相关类别）",
    )
    symptom_tier: Literal["frontend", "backend"] = Field(
        default="backend",
        description="症状表现层：用户看到的报错来自前端还是后端",
    )
    root_cause_tier: Literal["frontend", "backend", "data"] = Field(
        default="backend",
        description="根因所在层",
    )
    root_cause: str = Field(
        description="一句话根因（中文），具体到代码行为而非泛泛描述",
    )
    affected_file: str | None = Field(
        default=None,
        description="根因所在文件路径，如 app/services/task_service.py；无法定位填 null",
    )
    affected_function: str | None = Field(
        default=None,
        description="根因所在函数/方法名，如 create_comment、get_task_by_id；无法定位填 null",
    )
    fix_suggestion: str = Field(
        default="",
        description=(
            "修复建议，格式：【文件】...\\n【位置】第 N 行\\n"
            "【改前】...\\n【改后】...\\n【原因】..."
        ),
    )
    evidence_chain: list[str] = Field(
        default_factory=list,
        description="支撑根因的 evidence_ref 列表，如 ['sig-xxx']",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="置信度 0.0-1.0",
    )


_FORCED_FINAL_JSON_SCHEMA_HINT = (
    "{\n"
    '  "primary_category": "backend_error|frontend_crash|performance|logic|data|config",\n'
    '  "categories": ["..."],\n'
    '  "symptom_tier": "frontend|backend",\n'
    '  "root_cause_tier": "frontend|backend|data",\n'
    '  "root_cause": "一句话根因（中文）",\n'
    '  "affected_file": "path/to/file.py",\n'
    '  "affected_function": "function_name",\n'

    "  \"fix_suggestion\": \"【文件】...\\n【位置】第 N 行\\n【改前】...\\n"
    '【改后】...\\n【原因】...",\n'
    '  "evidence_chain": ["sig-xxx"],\n'
    '  "confidence": 0.85\n'
    "}"
)

_FORCED_FINAL_INSTRUCTION_CAP = (
    "你已达到工具调用上限，无法再调用任何工具。\n"
    "请基于上方对话历史中已收集到的所有证据，立即输出最终诊断报告 JSON。\n"
    "不要解释、不要重复证据、不要试图调用工具——只输出一个完整的 JSON 对象。\n\n"
    f"JSON schema（字段含义参考 system prompt）：\n{_FORCED_FINAL_JSON_SCHEMA_HINT}\n\n"
    "现在输出 JSON："
)

_FORCED_FINAL_INSTRUCTION_NARRATIVE = (
    "你刚才给出了诊断结论，但输出的是叙事性文字而非结构化 JSON。\n"
    "请把你刚才的结论立即格式化为下述 JSON 结构。\n"
    "不要做进一步调查、不要调用工具、不要解释——只输出一个完整的 JSON 对象。\n\n"
    f"JSON schema：\n{_FORCED_FINAL_JSON_SCHEMA_HINT}\n\n"
    "现在输出 JSON："
)


def _last_ai_has_json(messages: list[BaseMessage]) -> bool:
    """Return True if the last AIMessage contains extractable JSON.

    Used to decide whether the forced final JSON call is needed. Healthy
    cases (agent already emitted a JSON report) skip the extra call — no
    regression on the 11 healthy baseline cases.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = str(msg.content)
            if not content.strip():
                return False
            return _extract_json_from_text(content) is not None
    return False


def _last_ai_is_natural_stop(messages: list[BaseMessage]) -> bool:
    """Return True if the loop ended via natural stop (no tool_calls on last AIMessage)."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return not bool(getattr(msg, "tool_calls", None))
    return False


async def _forced_final_json_call(
    messages: list[BaseMessage],
    llm: BaseChatModel,
    invoke_config: dict[str, Any],
    natural_stop: bool,
    case_id: str,
    langfuse_handler: Any | None = None,
) -> AIMessage | None:
    """Make one final LLM call with ``with_structured_output`` enforcing the report schema.

    Iteration 2: instead of asking the LLM to emit free-text JSON and parsing
    it (which left room for unescaped quotes / trailing commas), we bind the
    ``ForcedDiagnosisReport`` schema as the ONLY tool on a fresh LLM instance
    via ``with_structured_output``. The model emits a single tool_call whose
    args match the schema; LangChain parses it into a Pydantic object. We
    then serialize it back to a JSON-string ``AIMessage`` so the downstream
    ``parse_diagnosis_report`` picks it up unchanged.

    Args:
        messages: Full ReAct loop message history (will be copied + appended to).
        llm: The diagnosis LLM — MUST be the un-bound version (no diagnostic
            ``bind_tools``). ``with_structured_output`` will bind exactly one
            tool (the report schema) on a derived instance; the diagnostic
            tools stay un-bound so DeepSeek can't fall back to emitting
            code_search / get_file_content tool_calls (v1 DSML trap).
        invoke_config: Langfuse callback config (so the forced call gets traced).
        natural_stop: True for failure mode 2 (narrative), False for mode 1 (cap).
        case_id: For logging.
        langfuse_handler: Optional Langfuse handler. When provided, the parsed
            Pydantic object (or failure context) is recorded as a
            ``structured_output_ForcedDiagnosisReport`` SPAN observation —
            making the Iteration 2 mechanism visible end-to-end in Langfuse.
            The callback path alone can't see the parsed object (LangChain
            materializes it from the tool_call args AFTER ``on_llm_end``
            fires), so this explicit record is the only way to inspect what
            the structured-output call actually produced.

    Returns:
        A synthetic ``AIMessage`` whose ``content`` is the JSON-serialized
        ``ForcedDiagnosisReport``, or None if the call itself failed (timeout
        / API error / model emitted no tool_call) — caller falls back to the
        existing ``parse_diagnosis_report`` fallback path in that case.
    """
    instruction = (
        _FORCED_FINAL_INSTRUCTION_NARRATIVE
        if natural_stop
        else _FORCED_FINAL_INSTRUCTION_CAP
    )
    forced_messages = list(messages) + [HumanMessage(content=instruction)]

    try:
        # method="function_calling" is CRITICAL for DeepSeek: the default
        # method ("json_schema") uses OpenAI's response_format structured
        # outputs, which DeepSeek rejects with
        #   400 - 'This response_format type is unavailable now'
        # DeepSeek DOES support tool calling (the ReAct loop uses it), so we
        # force the function_calling path — LangChain binds ForcedDiagnosisReport
        # as a single tool and parses the model's tool_call args.
        #
        # include_raw=True so we can log the raw LLM output when the model
        # fails to emit a matching tool_call — critical for diagnosing
        # regressions (e.g. if DeepSeek's tool-call support degrades).
        structured_llm = llm.with_structured_output(
            ForcedDiagnosisReport, method="function_calling", include_raw=True
        )
        result: dict[str, Any] = await asyncio.wait_for(
            structured_llm.ainvoke(
                forced_messages,
                config=invoke_config if invoke_config else None,  # type: ignore[arg-type]
            ),
            timeout=MAX_TIME_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "forced_final_json_call_failed",
            case_id=case_id,
            natural_stop=natural_stop,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.record_structured_output(
                    schema_name="ForcedDiagnosisReport",
                    parsed=None,
                    error=f"{type(exc).__name__}: {exc}",
                    case_id=case_id,
                )
        return None

    parsed: ForcedDiagnosisReport | None = (
        result.get("parsed") if isinstance(result, dict) else None
    )
    raw = result.get("raw") if isinstance(result, dict) else None
    if parsed is None:
        raw_content_str = str(getattr(raw, "content", "")) if raw is not None else None
        raw_tool_calls = getattr(raw, "tool_calls", None) if raw is not None else None

        logger.warning(
            "forced_final_json_call_no_tool_call",
            case_id=case_id,
            natural_stop=natural_stop,
            raw_content_preview=str(getattr(raw, "content", ""))[:500],
            raw_tool_calls=bool(getattr(raw, "tool_calls", None)),
        )
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.record_structured_output(
                    schema_name="ForcedDiagnosisReport",
                    parsed=None,
                    raw_content=raw_content_str,
                    raw_tool_calls=raw_tool_calls,
                    error="model emitted no matching tool_call (parsed=None)",
                    case_id=case_id,
                )
        return None

    # Serialize the parsed Pydantic object back to a JSON-string AIMessage so
    # parse_diagnosis_report (which expects content to be a JSON string) can
    # pick it up unchanged. model_dump_json produces properly-escaped JSON
    # by construction — no unescaped-quote risk.
    json_str = parsed.model_dump_json(indent=2)
    logger.info(
        "forced_final_json_call_completed",
        case_id=case_id,
        natural_stop=natural_stop,
        response_content_len=len(json_str),
        primary_category=parsed.primary_category,
        confidence=parsed.confidence,
        response_has_tool_calls=False,  # synthesized AIMessage has no tool_calls
    )
    # Record the parsed structured output to Langfuse. The callback path
    # (on_llm_end) captures the raw tool_call but NOT the parsed Pydantic
    # object — LangChain materializes it AFTER the callback fires. This
    # explicit span makes the Iteration 2 mechanism's actual output visible
    # in Langfuse (parsed fields + the JSON-serialized form that flows into
    # the final report).
    if langfuse_handler is not None:
        with contextlib.suppress(Exception):
            langfuse_handler.record_structured_output(
                schema_name="ForcedDiagnosisReport",
                parsed=parsed.model_dump(mode="json"),
                raw_content=str(getattr(raw, "content", "")) if raw is not None else None,
                raw_tool_calls=getattr(raw, "tool_calls", None) if raw is not None else None,
                case_id=case_id,
            )
    return AIMessage(content=json_str)
