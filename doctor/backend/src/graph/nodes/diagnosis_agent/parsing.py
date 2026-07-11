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

from src.graph.state import DiagnosisReport, Finding
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
            "affected_line": 42,
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
                affected_line=report_data.get("affected_line"),
                fix_suggestion=str(report_data.get("fix_suggestion", "")),
                evidence_chain=_ensure_str_list(report_data.get("evidence_chain", [])),
                confidence=float(report_data.get("confidence", 0.5)),
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

    # Fallback: construct a best-effort report from raw text
    logger.warning(
        "diagnosis_json_extraction_failed",
        content_len=len(last_ai_content),
        content_tail=last_ai_content[-300:] if len(last_ai_content) > 300 else last_ai_content,
    )
    return DiagnosisReport(
        primary_category="",
        root_cause=last_ai_content[:500] if last_ai_content else "（无法解析 Agent 输出）",
        confidence=0.2,
        notes="JSON 解析失败，使用原始输出作为 root_cause",
    )


def extract_findings(agent_result: dict[str, Any]) -> list[Finding]:
    """
    Extract Finding records from the agent's intermediate steps.

    Each AI message that contains a JSON block with finding-like fields
    is parsed as a Finding. This captures the agent's incremental reasoning.

    Args:
        agent_result: The full state dict from ``agent.ainvoke()``.

    Returns:
        List of Finding objects extracted from agent messages.
    """
    messages: list[Any] = agent_result.get("messages", [])
    findings: list[Finding] = []

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue

        content = str(msg.content)
        # Skip tool call messages (they have tool_calls, not meaningful findings)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            continue

        # Try to extract JSON from this message
        data = _extract_json_from_text(content)
        if data and ("summary" in data or "root_cause" in data):
            with contextlib.suppress(ValueError, TypeError):
                findings.append(
                    Finding(
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
                    )
                )

    return findings


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


def _ensure_str_list(value: Any) -> list[str]:
    """Ensure a value is a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if value and isinstance(value, str):
        return [value]
    return []
