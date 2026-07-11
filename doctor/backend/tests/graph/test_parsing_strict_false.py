"""
Unit tests for the JSON parser bugfix (Iteration 1 hotfix).

The forced final JSON call mechanism (Iteration 1) was producing correct JSON,
but ``_extract_json_from_text`` rejected it because ``json.loads(strict=True)``
(default) forbids literal control characters (newlines, tabs) inside string
values. LLMs frequently emit pretty-printed JSON with literal newlines inside
multi-line ``root_cause`` / ``fix_suggestion`` fields.

Fix: pass ``strict=False`` to all three ``json.loads`` call sites in parsing.py.

These tests verify:
1. ``_extract_json_from_text`` parses JSON with literal newlines in string values.
2. ``parse_diagnosis_report`` produces a valid DiagnosisReport from such JSON.
3. Markdown-fenced JSON with literal newlines is also parsed.
4. Brace-depth extraction handles literal newlines inside nested strings.
5. Regression: normal JSON (no literal control chars) still parses correctly.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from src.graph.nodes.diagnosis_agent.parsing import (
    _extract_json_by_depth,
    _extract_json_from_text,
    parse_diagnosis_report,
)


# ═════════════════════════════════════════════════════════════════════
# Reproduces the CONFIG-020 failure: pretty-printed JSON with literal
# newlines inside string values (root_cause, fix_suggestion).
# ═════════════════════════════════════════════════════════════════════

_PRETTY_JSON_WITH_LITERAL_NEWLINES = (
    '{\n'
    '  "primary_category": "config_error",\n'
    '  "categories": ["config_error"],\n'
    '  "symptom_tier": "backend",\n'
    '  "root_cause_tier": "backend",\n'
    '  "root_cause": "配置加载器在环境变量缺失时\n'
    '抛出 KeyError 而非降级到默认值,\n'
    '导致服务启动失败",\n'
    '  "affected_file": "app/config.py",\n'
    '  "affected_line": 28,\n'
    '  "fix_suggestion": "使用 os.getenv(key, default)\n'
    '并提供合理的 fallback 值",\n'
    '  "evidence_chain": ["sig-cfg-020"],\n'
    '  "confidence": 0.88\n'
    '}'
)


class TestStrictFalseBugfix:
    """Verify strict=False fixes the parsing failure for LLM-pretty-printed JSON."""

    def test_strict_true_rejects_literal_newlines(self) -> None:
        """Sanity check: the OLD behavior (strict=True) would fail."""
        with __import__("pytest").raises(json.JSONDecodeError):
            json.loads(_PRETTY_JSON_WITH_LITERAL_NEWLINES)

    def test_strict_false_accepts_literal_newlines(self) -> None:
        """The NEW behavior (strict=False) succeeds."""
        data = json.loads(_PRETTY_JSON_WITH_LITERAL_NEWLINES, strict=False)
        assert data["primary_category"] == "config_error"
        assert "\n" in data["root_cause"]

    def test_extract_json_from_text_parses_pretty_json(self) -> None:
        """_extract_json_from_text should parse JSON with literal newlines."""
        data = _extract_json_from_text(_PRETTY_JSON_WITH_LITERAL_NEWLINES)
        assert data is not None
        assert data["primary_category"] == "config_error"
        assert data["affected_line"] == 28
        assert "\n" in data["root_cause"]
        assert "\n" in data["fix_suggestion"]

    def test_extract_json_from_text_parses_markdown_fenced_pretty_json(self) -> None:
        """Markdown-fenced JSON with literal newlines should also parse."""
        text = "分析结论如下：\n```json\n" + _PRETTY_JSON_WITH_LITERAL_NEWLINES + "\n```"
        data = _extract_json_from_text(text)
        assert data is not None
        assert data["primary_category"] == "config_error"

    def test_extract_json_by_depth_handles_literal_newlines(self) -> None:
        """The brace-depth extractor must handle literal newlines in strings."""
        text = "前文...\n" + _PRETTY_JSON_WITH_LITERAL_NEWLINES + "\n后文..."
        data = _extract_json_by_depth(text)
        assert data is not None
        assert data["primary_category"] == "config_error"
        assert "\n" in data["root_cause"]


class TestParseDiagnosisReportWithLiteralNewlines:
    """End-to-end: parse_diagnosis_report should yield a valid report."""

    def test_parse_report_from_pretty_json(self) -> None:
        agent_result = {
            "messages": [AIMessage(content=_PRETTY_JSON_WITH_LITERAL_NEWLINES)],
        }
        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.primary_category == "config_error"
        assert report.affected_file == "app/config.py"
        assert report.affected_line == 28
        assert report.confidence == 0.88
        assert "\n" in report.root_cause

    def test_parse_report_from_narrative_plus_pretty_json(self) -> None:
        """LLM often emits reasoning text BEFORE the JSON block."""
        text = (
            "经过调查，配置加载器的根因已定位。\n"
            "以下是结构化报告：\n\n"
            + _PRETTY_JSON_WITH_LITERAL_NEWLINES
        )
        agent_result = {"messages": [AIMessage(content=text)]}
        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.primary_category == "config_error"
        assert report.affected_line == 28


class TestRegressionNormalJson:
    """Regression: JSON without literal control characters must still parse."""

    def test_compact_json_still_parses(self) -> None:
        compact = (
            '{"primary_category":"backend_error","categories":["backend_error"],'
            '"symptom_tier":"backend","root_cause_tier":"backend",'
            '"root_cause":"scalar_one 500","affected_file":"app/api/tasks.py",'
            '"affected_line":83,"fix_suggestion":"改用 scalar_one_or_none",'
            '"evidence_chain":["sig-be-021"],"confidence":0.9}'
        )
        data = _extract_json_from_text(compact)
        assert data is not None
        assert data["primary_category"] == "backend_error"

    def test_escaped_newlines_still_parse(self) -> None:
        """JSON with properly-escaped \\n inside strings works in both modes."""
        text = (
            '{"root_cause":"line1\\nline2","primary_category":"x","confidence":0.5}'
        )
        data = _extract_json_from_text(text)
        assert data is not None
        assert data["root_cause"] == "line1\nline2"
