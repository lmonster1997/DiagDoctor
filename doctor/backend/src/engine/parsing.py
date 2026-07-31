"""Parse agent JSON output into DiagnosisReport + Finding records.

Strategies for extracting JSON from LLM text (tried in order):
1. Markdown code fences (```json ... ``` or ``` ... ```)
2. Brace-depth tracking — handles arbitrary nesting + braces inside string values
3. Fallback: json.loads on the whole text
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from langchain_core.messages import AIMessage

from src.engine.state import DiagnosisReport, Finding
from src.observability.logger import get_logger

logger = get_logger(__name__)


def parse_diagnosis_report(agent_result: dict[str, Any]) -> DiagnosisReport | None:
    """
    Parse the DiagnosisAgent's final output into a DiagnosisReport.

    Extracts JSON from the last AI message. The agent is instructed to
    output structured JSON matching the DiagnosisReport schema.

    Expected JSON format::

        {
            "primary_category": "backend_error",
            "categories": ["backend_error", "performance"],
            "symptom_tier": "frontend",
            "root_cause_tier": "backend",
            "root_cause": "...",
            "affected_file": "app/services/task_service.py",
            "affected_function": "create_task",
            "fix_suggestion": "...",
            "evidence_chain": ["sig-xxx"],
            "confidence": 0.85
        }

    Args:
        agent_result: The full state dict returned by ``agent.ainvoke()``.

    Returns:
        DiagnosisReport if parsing succeeded, None otherwise.
    """
    messages: list[Any] = agent_result.get("messages", [])

    # Find the last AI message
    last_ai_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai_content = str(msg.content)
            break

    if not last_ai_content:
        logger.warning("no_ai_message_in_agent_result")
        return None

    # Try to extract JSON from the response
    report_data = _extract_json_from_text(last_ai_content)

    if report_data:
        try:
            parsed_report = DiagnosisReport(
                primary_category=str(report_data.get("primary_category", "")),
                categories=_ensure_str_list(report_data.get("categories", [])),
                symptom_tier=report_data.get("symptom_tier", "backend"),
                root_cause_tier=report_data.get("root_cause_tier", "backend"),
                root_cause=str(report_data.get("root_cause", "")),
                affected_file=report_data.get("affected_file"),
                affected_function=report_data.get("affected_function"),
                fix_suggestion=str(report_data.get("fix_suggestion", "")),
                evidence_chain=_ensure_str_list(report_data.get("evidence_chain", [])),
                confidence=float(report_data.get("confidence", 0.5)),
                referenced_case_ids=_ensure_str_list(report_data.get("referenced_case_ids", [])),
            )
            logger.info(
                "diagnosis_report_parsed",
                primary_category=parsed_report.primary_category,
                categories=parsed_report.categories,
                confidence=parsed_report.confidence,
                affected_file=parsed_report.affected_file,
            )
            return parsed_report
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "failed_to_parse_diagnosis_report",
                error=str(exc),
                content_preview=last_ai_content[:500],
                extracted_json_keys=list(report_data.keys()) if report_data else [],
            )

    # No JSON extractable -> return None. The caller (_finalize_report_for_dict_state)
    # falls back to a findings-based root_cause, which is far better than the old
    # behavior of stuffing the last AIMessage's mid-reasoning text into root_cause.
    # That produced garbage reports whenever the agent was cut off mid-investigation
    # (budget exhausted, last AIMessage a tool-call with no JSON) -- e.g. root_cause
    # = "关键发现！有多个 task 的 assignee_id 是空字符串...". Findings (record_hypothesis
    # records / root_cause) are the durable signal; use them.
    logger.warning(
        "diagnosis_json_extraction_failed",
        content_len=len(last_ai_content),
        content_tail=last_ai_content[-300:] if len(last_ai_content) > 300 else last_ai_content,
    )
    return None


def extract_findings(agent_result: dict[str, Any]) -> list[Finding]:
    """Extract Finding records from the agent's messages.

    §7.2 hypothesis-tree support: scans ALL AIMessages (including tool-call
    ones, which ReAct reasoning lives in) for ``{"hypothesis":..., "status":...,
    "evidence":..., "refuted":...}`` blocks the agent emits as it
    confirms/excludes hypotheses. The final report's ``root_cause`` becomes a
    ``confirmed`` finding. Findings are deduplicated by summary, keeping the
    latest status (so a hypothesis that goes pending -> excluded is recorded
    as excluded). Pre-§7.2 output (no hypothesis blocks) degrades gracefully:
    findings is just the final root_cause (confirmed), as before.

    Args:
        agent_result: The full state dict from ``agent.ainvoke()``.

    Returns:
        List of Finding objects (with ``status``/``refuted``/``refutation_evidence``
        populated when the agent emitted hypothesis blocks).
    """
    messages: list[Any] = agent_result.get("messages", [])
    findings: list[Finding] = []

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        has_tool_calls = bool(getattr(msg, "tool_calls", None))
        # §7.2 主路径:agent 调用 record_hypothesis 工具记录假设(原生 tool-calling
        # agent 的可靠结构化通道;content 里的自由文本 JSON 它不会 emit)。
        for tc in getattr(msg, "tool_calls", None) or []:
            if _tool_call_name(tc) == "record_hypothesis":
                finding = _finding_from_json(_tool_call_args(tc), has_tool_calls=True)
                if finding is not None:
                    findings.append(finding)
        # §7.2 fallback:content 里自由文本 JSON 假设块(真 tool-calling agent 不会走,
        # 但保留兼容;_finding_from_json 的 root_cause/summary 分支仍处理最终报告)。
        for data in _extract_all_json_objects(str(msg.content)):
            finding = _finding_from_json(data, has_tool_calls)
            if finding is not None:
                findings.append(finding)

    return _dedup_findings_by_summary(findings)


def _tool_call_name(tc: Any) -> str:
    """Best-effort tool name from a ToolCall (dict or object)."""
    if isinstance(tc, dict):
        return str(tc.get("name", ""))
    return str(getattr(tc, "name", ""))


def _tool_call_args(tc: Any) -> dict[str, Any]:
    """Best-effort args dict from a ToolCall (dict or object)."""
    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
    return dict(args) if isinstance(args, dict) else {}


_VALID_STATUSES = {"confirmed", "excluded", "pending"}


def _finding_from_json(data: dict[str, Any], has_tool_calls: bool) -> Finding | None:
    """Build a Finding from one parsed JSON object, or None if not finding-shaped.

    - ``hypothesis`` key -> a hypothesis-block finding (with status/refuted).
      Parsed from any message (tool-call reasoning or final report).
    - ``root_cause`` key -> the final report's converged root cause (confirmed).
      Only from non-tool-call messages (the final report) to avoid spurious
      matches in reasoning text.
    - ``summary`` key -> a generic finding (pending). Same non-tool-call guard.
    """
    # Hypothesis block (emitted mid-investigation per §7.2 prompt discipline).
    if "hypothesis" in data:
        status = str(data.get("status", "pending"))
        if status not in _VALID_STATUSES:
            status = "pending"
        refuted = bool(data.get("refuted", status == "excluded"))
        with contextlib.suppress(ValueError, TypeError):
            return Finding(
                agent="diagnosis_agent",
                summary=str(data.get("hypothesis", "")),
                status=status,  # type: ignore[arg-type]
                refuted=refuted,
                refutation_evidence=str(data.get("evidence", "")) if refuted else "",
                evidence_refs=_ensure_str_list(data.get("evidence_refs", [])),
                confidence=float(data.get("confidence", 0.5)),
            )
        return None

    # Final-report fields only from non-tool-call messages (avoid picking up
    # spurious summary/root_cause JSON in reasoning text).
    if has_tool_calls:
        return None
    if not (data.get("summary") or data.get("root_cause")):
        return None

    is_root_cause = "root_cause" in data and data.get("root_cause")
    with contextlib.suppress(ValueError, TypeError):
        return Finding(
            agent="diagnosis_agent",
            summary=str(data.get("summary", data.get("root_cause", ""))),
            evidence_refs=_ensure_str_list(
                data.get("evidence_refs", data.get("evidence_chain", []))
            ),
            affected_files=_ensure_str_list(
                data.get("affected_files", [data.get("affected_file", "")])
            ),
            fix_suggestion=str(data.get("fix_suggestion", "")),
            confidence=float(data.get("confidence", 0.5)),
            status="confirmed" if is_root_cause else "pending",
        )
    return None


def _dedup_findings_by_summary(findings: list[Finding]) -> list[Finding]:
    """Dedup by summary text, keeping the latest status (last occurrence wins).

    Preserves first-occurrence order. A hypothesis that appears as ``pending``
    then later ``excluded`` is recorded as ``excluded``.
    """
    by_summary: dict[str, Finding] = {}
    for f in findings:
        key = f.summary.strip()
        if not key:
            continue
        by_summary[key] = f  # later overwrites -> latest status
    return list(by_summary.values())


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from text (handles markdown code fences, raw JSON,
    mixed natural-language+JSON output, and nested structures).

    Strategy (tried in order):
    1. Markdown code fences (```json ... ``` or ``` ... ```)
    2. Brace-depth tracking — finds the FIRST complete JSON object by counting
       depth, respecting string escapes. Handles arbitrary nesting and braces
       inside string values.
    3. Fallback: greedy scan for any balanced ``{...}`` candidate.
    """
    # ── 1. Markdown code fences ──────────────────────────────────
    json_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match, strict=False)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    # ── 2. Brace-depth tracking (handles arbitrary nesting + braces in strings) ──
    result = _extract_json_by_depth(text)
    if result is not None:
        return result

    # ── 3. Fallback: json.loads on the whole text (in case it's pure JSON) ──
    try:
        return json.loads(text, strict=False)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    return None


def _extract_json_by_depth(text: str) -> dict[str, Any] | None:
    """Extract JSON object(s) from text using brace-depth tracking.

    Unlike regex, this correctly handles:
    - Arbitrary nesting depth
    - Braces inside JSON string values (e.g. ``{"code": "if (x) { return; }"}``)
    - Multiple JSON candidates (tries each, returns the first valid one)

    Also tries the LAST JSON object first (LLMs tend to put JSON at the end
    after natural-language reasoning).
    """
    # Collect all { } spans with their depth
    candidates: list[tuple[int, int]] = []  # (start, end) pairs
    depth = 0
    in_string = False
    escape_next = False
    start = -1

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append((start, i + 1))
                start = -1

    if not candidates:
        return None

    # Try candidates in reverse order (last JSON object first —
    # LLMs typically output reasoning before JSON, so the last
    # ``{...}`` block is most likely the intended structured output).
    for start, end in reversed(candidates):
        candidate = text[start:end]
        try:
            return json.loads(candidate, strict=False)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    return None


def _extract_all_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract ALL top-level JSON objects from text (brace-depth tracking).

    Unlike ``_extract_json_from_text`` (which returns one), this returns every
    balanced ``{...}`` span that parses as a dict. Used by ``extract_findings``
    to capture multiple §7.2 hypothesis blocks the agent may emit in one
    reasoning turn. Same brace-depth approach as ``_extract_json_by_depth``
    (handles arbitrary nesting + braces inside string values).
    """
    candidates: list[tuple[int, int]] = []  # (start, end) pairs
    depth = 0
    in_string = False
    escape_next = False
    start = -1

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append((start, i + 1))
                start = -1

    results: list[dict[str, Any]] = []
    for s, e in candidates:
        try:
            obj = json.loads(text[s:e], strict=False)
            if isinstance(obj, dict):
                results.append(obj)
        except json.JSONDecodeError:
            continue
    return results


def _ensure_str_list(value: Any) -> list[str]:
    """Ensure a value is a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if value and isinstance(value, str):
        return [value]
    return []


def clamp_referenced_case_ids(referenced: list[str], retrieved: list[str]) -> list[str]:
    """Clamp agent-declared ``referenced_case_ids`` to ⊆ ``retrieved`` (§8.1).

    The agent outputs ``referenced_case_ids`` in its final JSON, but it may
    hallucinate ids it was never given (or copy a stale id from elsewhere).
    The diagnosis_agent node passes the cases actually retrieved this run
    (``retrieved_case_ids``) and this function discards any referenced id not
    in that set -- the agent can only cite cases it was shown. Order is
    preserved (agent's ordering) and duplicates are dropped.

    Called in the node (NOT in ``parse_diagnosis_report``): parsing stays a
    pure JSON->object step; the trust-boundary clamp is an orchestration
    policy that needs ``retrieved_case_ids``, which the parser doesn't have.
    """
    allowed = set(retrieved)
    seen: set[str] = set()
    out: list[str] = []
    for case_id in referenced:
        cid = str(case_id) if case_id is not None else ""
        if cid and cid in allowed and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out
