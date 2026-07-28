"""Unit tests for §7.2 HITL scratchpad: hypothesis-tree extraction + rendering.

Covers:
- ``extract_findings`` parses ``{"hypothesis":..., "status":..., "evidence":...,
  "refuted":...}`` blocks from tool-call AIMessages (ReAct reasoning), with
  status/refuted/refutation_evidence populated.
- final report ``root_cause`` -> ``confirmed`` finding.
- dedup by summary keeps the latest status (pending -> confirmed).
- graceful degradation: no hypothesis blocks -> just the confirmed root_cause.
- ``_format_scratchpad`` renders the 3-section tree (已确认/已排除/待验证),
  omits empty sections, falls back to "(暂无)".
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.engine.nodes.diagnosis_agent import _format_scratchpad
from src.engine.parsing import extract_findings
from src.engine.state import Finding


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════


def _ai(content: str, tool_call_id: str | None = None) -> AIMessage:
    """AIMessage optionally carrying a tool_call (ReAct reasoning step)."""
    if tool_call_id is None:
        return AIMessage(content=content)
    return AIMessage(
        content=content,
        tool_calls=[{"name": "search_observability", "args": {}, "id": tool_call_id, "type": "tool_call"}],
    )


def _hypothesis(h: str, status: str, evidence: str = "", refuted: bool | None = None) -> str:
    """A hypothesis block as the agent emits it mid-reasoning."""
    r = "true" if (refuted if refuted is not None else status == "excluded") else "false"
    return (
        '{"hypothesis": "' + h + '", "status": "' + status + '", '
        '"evidence": "' + evidence + '", "refuted": ' + r + "}"
    )


_REPORT_JSON = (
    '{"primary_category": "backend_error", "root_cause": "GetImageByUID fails on export", '
    '"evidence_chain": ["sig-1", "span-9"], "confidence": 0.85}'
)


# ═════════════════════════════════════════════════════════════════════
# extract_findings
# ═════════════════════════════════════════════════════════════════════


class TestExtractFindingsHypothesis:
    def test_hypothesis_block_from_tool_call_message(self) -> None:
        """ReAct reasoning (tool-call AIMessage) carries hypothesis blocks -
        extract_findings must NOT skip these (the §7.2 root change)."""
        msgs = [_ai("check fenix. " + _hypothesis("fenix is the failure side", "excluded", "fenix 200 OK"), "tc1")]
        findings = extract_findings({"messages": msgs})
        assert len(findings) == 1
        f = findings[0]
        assert f.summary == "fenix is the failure side"
        assert f.status == "excluded"
        assert f.refuted is True
        assert f.refutation_evidence == "fenix 200 OK"

    def test_multiple_hypothesis_blocks_in_one_message(self) -> None:
        content = (
            _hypothesis("fenix is the failure side", "excluded", "fenix 200 OK")
            + "\nthen export. "
            + _hypothesis("GetImageByUID fails on export", "pending", "trace-xxx")
        )
        findings = extract_findings({"messages": [_ai(content, "tc1")]})
        assert len(findings) == 2
        by_summary = {f.summary: f for f in findings}
        assert by_summary["fenix is the failure side"].status == "excluded"
        assert by_summary["GetImageByUID fails on export"].status == "pending"

    def test_final_report_root_cause_is_confirmed(self) -> None:
        msgs = [_ai(_REPORT_JSON)]  # no tool_calls -> final report
        findings = extract_findings({"messages": msgs})
        assert len(findings) == 1
        f = findings[0]
        assert f.summary == "GetImageByUID fails on export"
        assert f.status == "confirmed"
        assert f.refuted is False
        assert f.evidence_refs == ["sig-1", "span-9"]

    def test_dedup_keeps_latest_status(self) -> None:
        """A hypothesis that appears as pending then later confirmed is recorded
        as confirmed (last occurrence wins)."""
        msgs = [
            _ai(_hypothesis("GetImageByUID fails on export", "pending", "trace-xxx"), "tc1"),
            _ai(_hypothesis("GetImageByUID fails on export", "confirmed", "trace ERROR"), "tc2"),
        ]
        findings = extract_findings({"messages": msgs})
        assert len(findings) == 1
        assert findings[0].status == "confirmed"

    def test_dedup_pending_then_excluded(self) -> None:
        msgs = [
            _ai(_hypothesis("fenix is the failure side", "pending"), "tc1"),
            _ai(_hypothesis("fenix is the failure side", "excluded", "200 OK"), "tc2"),
        ]
        findings = extract_findings({"messages": msgs})
        assert len(findings) == 1
        assert findings[0].status == "excluded"
        assert findings[0].refutation_evidence == "200 OK"

    def test_no_hypothesis_blocks_degrades_to_confirmed_root_cause(self) -> None:
        """Pre-§7.2 output (no hypothesis blocks): findings is just the final
        root_cause (confirmed). No regression."""
        reasoning = _ai("let me check the logs...", "tc1")  # plain text, no JSON
        report = _ai(_REPORT_JSON)
        findings = extract_findings({"messages": [reasoning, report]})
        assert len(findings) == 1
        assert findings[0].status == "confirmed"
        assert findings[0].summary == "GetImageByUID fails on export"

    def test_invalid_status_defaults_to_pending(self) -> None:
        block = '{"hypothesis": "weird one", "status": "bogus", "evidence": "", "refuted": false}'
        findings = extract_findings({"messages": [_ai(block, "tc1")]})
        assert len(findings) == 1
        assert findings[0].status == "pending"

    def test_refuted_defaults_from_status_when_omitted(self) -> None:
        """If the agent omits ``refuted``, infer it from status (excluded -> True)."""
        block = '{"hypothesis": "h1", "status": "excluded", "evidence": "counter"}'
        findings = extract_findings({"messages": [_ai(block, "tc1")]})
        assert findings[0].refuted is True
        assert findings[0].refutation_evidence == "counter"

    def test_non_ai_messages_ignored(self) -> None:
        """ToolMessages / HumanMessages are never parsed for findings."""
        msgs = [
            HumanMessage(content=_hypothesis("h", "confirmed")),
            _ai(" reasoning ", "tc1"),
            ToolMessage(content="tool result", tool_call_id="tc1", name="search_observability"),
            _ai(_REPORT_JSON),
        ]
        findings = extract_findings({"messages": msgs})
        # only the final report root_cause (the HumanMessage hypothesis is ignored)
        assert len(findings) == 1
        assert findings[0].status == "confirmed"

    def test_empty_messages(self) -> None:
        assert extract_findings({"messages": []}) == []


# ═════════════════════════════════════════════════════════════════════
# _format_scratchpad
# ═════════════════════════════════════════════════════════════════════


class TestFormatScratchpad:
    def test_three_sections_grouped_by_status(self) -> None:
        findings = [
            Finding(summary="GetImageByUID fails on export", status="confirmed", evidence_refs=["sig-1"]),
            Finding(summary="fenix is the failure side", status="excluded", refuted=True, refutation_evidence="fenix 200 OK"),
            Finding(summary="PixelSpacing polluted", status="pending", evidence_refs=["trace-x"]),
        ]
        out = _format_scratchpad(findings)
        assert "已确认事实" in out
        assert "GetImageByUID fails on export" in out
        assert "已排除假设" in out
        assert "fenix is the failure side" in out
        assert "fenix 200 OK" in out  # refutation evidence surfaced
        assert "待验证线索" in out
        assert "PixelSpacing polluted" in out

    def test_empty_sections_omitted(self) -> None:
        findings = [Finding(summary="only confirmed", status="confirmed", evidence_refs=["s1"])]
        out = _format_scratchpad(findings)
        assert "已确认事实" in out
        assert "已排除假设" not in out
        assert "待验证线索" not in out

    def test_excluded_only(self) -> None:
        findings = [Finding(summary="dead end", status="excluded", refuted=True, refutation_evidence="counterexample")]
        out = _format_scratchpad(findings)
        assert "已排除假设" in out
        assert "dead end" in out
        assert "counterexample" in out
        assert "已确认事实" not in out

    def test_empty_findings_returns_placeholder(self) -> None:
        assert _format_scratchpad([]) == "(暂无)"

    def test_all_empty_summaries_returns_placeholder(self) -> None:
        findings = [Finding(summary="", status="confirmed")]
        assert _format_scratchpad(findings) == "(暂无)"

    def test_default_status_finding_goes_to_pending(self) -> None:
        """A Finding constructed without status (default) lands in 待验证."""
        findings = [Finding(summary="legacy finding")]
        out = _format_scratchpad(findings)
        assert "待验证线索" in out
        assert "legacy finding" in out
