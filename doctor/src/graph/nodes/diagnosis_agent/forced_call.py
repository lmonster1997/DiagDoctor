"""Iteration 1: forced final JSON call mechanism.

Baseline (Iteration 0) showed two failure modes accounting for all disaster
cases:
  mode 1 (3/4 disaster): loop hits MAX_TOOL_CALLS cap, last AIMessage has
    content="" + tool_calls=[...] → parse_diagnosis_report returns a
    low-confidence fallback with empty root_cause.
  mode 2 (1/4 disaster + 2 regression cases): agent natural-stops but
    emits narrative prose without any JSON structure → same fallback.

Single mechanism covers both: after the ReAct loop ends, if the last
AIMessage does not contain extractable JSON, make ONE extra LLM call
WITHOUT bind_tools so the model has no tool surface to fall back on
(this is the v1 REPORTING-phase failure mode — DeepSeek keeps emitting
DSML tool-call markers when tools are bound). Forced call asks the LLM
to format the conclusion as JSON immediately.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from src.graph.nodes.diagnosis_agent.constants import MAX_TIME_SECONDS, MAX_TOKENS_BUDGET
from src.graph.nodes.diagnosis_agent.parsing import _extract_json_from_text
from src.observability.logger import get_logger

logger = get_logger(__name__)


_FORCED_FINAL_JSON_SCHEMA_HINT = (
    "{\n"
    '  "primary_category": "backend_error|frontend_crash|performance|logic|data|config",\n'
    '  "categories": ["..."],\n'
    '  "symptom_tier": "frontend|backend",\n'
    '  "root_cause_tier": "frontend|backend|data",\n'
    '  "root_cause": "一句话根因（中文）",\n'
    '  "affected_file": "path/to/file.py",\n'
    '  "affected_line": 42,\n'
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
) -> AIMessage | None:
    """Make one final LLM call WITHOUT tools bound, forcing JSON output.

    Args:
        messages: Full ReAct loop message history (will be copied + appended to).
        llm: The diagnosis LLM — MUST be the un-bound version (no ``bind_tools``).
            Calling with the tool-bound version would let the LLM emit more
            ``tool_calls`` and we'd be back at v1's DSML trap.
        invoke_config: Langfuse callback config (so the forced call gets traced).
        natural_stop: True for failure mode 2 (narrative), False for mode 1 (cap).
        case_id: For logging.

    Returns:
        The forced AIMessage (content should contain JSON), or None if the
        call itself failed (timeout / API error) — caller falls back to
        existing parse_diagnosis_report fallback in that case.
    """
    instruction = (
        _FORCED_FINAL_INSTRUCTION_NARRATIVE
        if natural_stop
        else _FORCED_FINAL_INSTRUCTION_CAP
    )
    forced_messages = list(messages) + [HumanMessage(content=instruction)]

    try:
        response: AIMessage = await asyncio.wait_for(
            llm.ainvoke(
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
        )
        return None

    logger.info(
        "forced_final_json_call_completed",
        case_id=case_id,
        natural_stop=natural_stop,
        response_content_len=len(str(response.content)),
        response_has_tool_calls=bool(getattr(response, "tool_calls", None)),
    )
    return response


async def _maybe_forced_final_json_call(
    messages: list[BaseMessage],
    llm: BaseChatModel,
    ctx_budget: Any,
    invoke_config: dict[str, Any],
    case_id: str,
    budget_exhausted: bool = False,
) -> bool:
    """Gate + call wrapper for the forced final JSON mechanism.

    Skips when:
    - last AIMessage already has extractable JSON (healthy case, no regression)
    - token budget already blown (forced call would fail anyway)
    - messages is empty (loop never produced a message)

    When triggered, appends the forced response to ``messages`` (in place)
    and updates ``ctx_budget`` token accounting.

    Args:
        messages: ReAct loop message history — mutated in place when forced
            call succeeds (forced response appended).
        llm: Un-bound diagnosis LLM (NOT llm_with_tools).
        ctx_budget: ContextBudget for token accounting (must support
            ``total_used`` attribute and ``add_agent_reasoning`` method).
        invoke_config: Langfuse callback config.
        case_id: For logging.
        budget_exhausted: Whether the ReAct loop hit a cap — included in the
            trigger log line for trace parity with the original inline logic.

    Returns:
        True if the forced call was triggered (regardless of whether the call
        itself succeeded), False if skipped by the gate.
    """
    if (
        not _last_ai_has_json(messages)
        and ctx_budget.total_used < MAX_TOKENS_BUDGET
        and messages  # nothing to feed the LLM if loop never produced a message
    ):
        natural_stop = _last_ai_is_natural_stop(messages)
        logger.info(
            "forced_final_json_call_triggered",
            case_id=case_id,
            natural_stop=natural_stop,
            budget_exhausted=budget_exhausted,
            last_ai_has_json=False,
        )
        forced_response = await _forced_final_json_call(
            messages=messages,
            llm=llm,  # NB: un-bound LLM — no tools surface, avoids v1 DSML trap
            invoke_config=invoke_config,
            natural_stop=natural_stop,
            case_id=case_id,
        )
        if forced_response is not None:
            messages.append(forced_response)
            ctx_budget.add_agent_reasoning(str(forced_response.content))
        return True
    return False
