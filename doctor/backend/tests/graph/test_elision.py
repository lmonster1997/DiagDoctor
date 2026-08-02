"""Unit tests for §7.1 symbol-elision: ``build_elision_placeholder`` +
``ContextElisionMiddleware``.

Covers:
- placeholder construction per tool type (obs/code/db/generic) + JSON-broken
  fallback (re-fetch handle from call args) + empty content.
- middleware ``abefore_model``: only ToolMessages older than ``keep_recent``
  are elided; same-id in-place replacement (count unchanged after reducer);
  AIMessage/SystemMessage/HumanMessage untouched; tool_call_id preserved so the
  preceding AIMessage's tool_calls still associate; gated by settings.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from src.engine.context.elision import build_elision_placeholder
from src.engine.middleware.context_elision import (
    ContextElisionMiddleware,
    _index_tool_call_args,
)
from src.engine.run_context import DiagnosisRunContext

# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════


def _obs_result(
    *,
    source: str = "loki",
    query: str = '{app="demo"}',
    summary: str | None = "GetImageByUID 失败",
    insights: str | None = None,
    span_count: int = 3,
) -> str:
    """Build a search_observability result JSON (echoes query/time_range/analysis)."""
    analysis: dict[str, Any] = {"span_count": span_count, "root_count": 1}
    if summary is not None:
        analysis["summary"] = summary
    resp = {
        "source": source,
        "query": query,
        "time_range": {"start": "2026-07-28T10:00:00", "end": "2026-07-28T10:05:00"},
        "logs": [],
        "traces": [],
        "analysis": analysis,
        "metadata": {},
        "frontend_errors": [],
        "insights": insights or "",
    }
    return json.dumps(resp, ensure_ascii=False)


def _ai_with_tool_call(name: str, args: dict[str, Any], tc_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": tc_id, "type": "tool_call"}]
    )


def _tool_msg(tc_id: str, name: str, content: str, msg_id: str | None = None) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tc_id, name=name, id=msg_id)


def _result(n: int) -> str:
    """Tool result = a useful first line (key finding) + a large bulk body.

    The placeholder keeps the first line and drops the bulk, so tests can assert
    the bulk is gone while the key finding survives.
    """
    return f"header_line_{n}\n" + f"BULK{n}" * 500


def _patch_settings():
    """Patch the settings object imported into context_elision middleware.

    Tests set ``context_elision_enabled`` / ``context_elision_keep_recent``
    explicitly on the mock for clarity (mirrors test_middleware.py's truncation
    tests).
    """
    return patch("src.engine.middleware.context_elision.settings")


def _make_runtime(ctx: DiagnosisRunContext | None = None) -> Runtime:
    """Build a Runtime whose ``.context`` is ``ctx`` (or None). Stand-in for
    langgraph's per-invocation Runtime so middlewares can read ``runtime.context``."""
    return Runtime(context=ctx)


# ═════════════════════════════════════════════════════════════════════
# build_elision_placeholder
# ═════════════════════════════════════════════════════════════════════


class TestBuildElisionPlaceholder:
    def test_obs_includes_refetch_handle_and_insights(self) -> None:
        content = _obs_result(insights="N+1 in fetch loop", summary="GetImageByUID 失败")
        out = build_elision_placeholder("search_observability", {"source": "loki"}, content)
        # re-fetch handle carries source + query + resolved time_range
        assert 'source="loki"' in out
        assert '{app="demo"}' in out  # query value (placeholder doesn't escape inner quotes)
        assert 'start="2026-07-28T10:00:00"' in out
        assert 'end="2026-07-28T10:05:00"' in out
        # insights preferred over analysis.summary as the key finding
        assert "N+1 in fetch loop" in out
        assert "[已归档·可重取]" in out

    def test_obs_falls_back_to_analysis_summary_when_no_insights(self) -> None:
        content = _obs_result(summary="GetImageByUID 失败", insights=None)
        out = build_elision_placeholder("search_observability", {"source": "loki"}, content)
        assert "GetImageByUID 失败" in out

    def test_obs_structural_finding_when_no_summary(self) -> None:
        content = _obs_result(summary=None, insights=None, span_count=42)
        out = build_elision_placeholder("search_observability", {"source": "loki"}, content)
        assert "span 42" in out

    def test_obs_broken_json_uses_call_args_for_refetch_handle(self) -> None:
        """§7.6 worst case: JSON broken by head/tail truncation -> re-fetch
        handle from call args (still re-fetchable), finding from first key line."""
        broken = '{"source":"tempo","query":"trace-123",... [truncated by head/tail'
        args = {"source": "tempo", "query": "trace-123", "start": "s", "end": "e"}
        out = build_elision_placeholder("search_observability", args, broken)
        assert 'source="tempo"' in out
        assert 'query="trace-123"' in out
        assert 'start="s"' in out
        assert "[已归档·可重取]" in out

    def test_code_search_handle_carries_query(self) -> None:
        out = build_elision_placeholder(
            "code_search", {"query": "create_task"}, "file.py:42\ndef create_task():"
        )
        assert 'code_search(query="create_task")' in out
        assert "file.py:42" in out

    def test_get_file_content_handle_carries_path_and_lines(self) -> None:
        out = build_elision_placeholder(
            "get_file_content",
            {"file_path": "app/svc.py", "start_line": 10, "end_line": 20},
            "line 10\nline 11",
        )
        assert 'file_path="app/svc.py"' in out
        assert "start_line=10" in out
        assert "end_line=20" in out

    def test_db_query_handle_carries_sql(self) -> None:
        out = build_elision_placeholder(
            "db_query", {"sql": "SELECT * FROM users"}, "id=1 name=alice"
        )
        assert 'sql="SELECT * FROM users"' in out
        assert "id=1 name=alice" in out

    def test_unknown_tool_uses_generic_handle(self) -> None:
        out = build_elision_placeholder(
            "inspect_frontend_error", {"browser_errors": "err"}, "first line\nsecond"
        )
        assert "inspect_frontend_error(" in out
        assert "first line" in out

    def test_empty_content_yields_empty_result_finding(self) -> None:
        out = build_elision_placeholder("code_search", {"query": "x"}, "")
        assert "(空结果)" in out

    def test_long_finding_is_clipped(self) -> None:
        content = _obs_result(insights="x" * 1000)
        out = build_elision_placeholder("search_observability", {}, content)
        # finding line is clipped (well under 1000 chars on that line)
        finding_line = [ln for ln in out.splitlines() if ln.startswith("关键发现:")][0]
        assert len(finding_line) < 400


# ═════════════════════════════════════════════════════════════════════
# _index_tool_call_args
# ═════════════════════════════════════════════════════════════════════


class TestIndexToolCallArgs:
    def test_indexes_args_by_tool_call_id(self) -> None:
        msgs = [
            _ai_with_tool_call("code_search", {"query": "create_task"}, "tc1"),
            _tool_msg("tc1", "code_search", "result"),
        ]
        assert _index_tool_call_args(msgs) == {"tc1": {"query": "create_task"}}

    def test_multiple_tool_calls_indexed(self) -> None:
        msgs = [
            _ai_with_tool_call("code_search", {"query": "a"}, "tc1"),
            _ai_with_tool_call("code_search", {"query": "b"}, "tc2"),
        ]
        assert _index_tool_call_args(msgs) == {
            "tc1": {"query": "a"},
            "tc2": {"query": "b"},
        }

    def test_empty_messages_returns_empty_index(self) -> None:
        assert _index_tool_call_args([]) == {}


# ═════════════════════════════════════════════════════════════════════
# ContextElisionMiddleware.abefore_model
# ═════════════════════════════════════════════════════════════════════


class TestContextElisionMiddleware:
    async def test_elides_only_oldest_beyond_keep_recent(self) -> None:
        """4 tool calls, keep_recent=3 -> only the oldest (tc1) is elided."""
        raw = [
            SystemMessage(content="sys"),
            HumanMessage(content="evidence"),
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
            _ai_with_tool_call("code_search", {"query": "q4"}, "tc4"),
            _tool_msg("tc4", "code_search", _result(4)),
        ]
        messages = add_messages([], raw)  # assigns ids (as in the real loop)
        tc1_msg = next(
            m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
        )

        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())

        assert result is not None
        replacements = result["messages"]
        assert len(replacements) == 1  # only tc1
        repl = replacements[0]
        assert isinstance(repl, ToolMessage)
        # same id -> add_messages will replace in place (not duplicate)
        assert repl.id == tc1_msg.id
        assert repl.tool_call_id == "tc1"  # preserved -> AIMessage tool_call still associates
        assert repl.name == "code_search"
        assert "[已归档·可重取]" in str(repl.content)
        assert "header_line_1" in str(repl.content)  # key finding kept
        assert "BULK1" not in str(repl.content)  # raw bulk dropped

    async def test_reducer_replaces_in_place_no_duplicate(self) -> None:
        """End-to-end: applying the returned replacements via add_messages keeps
        the message count unchanged and swaps the content in place."""
        raw = [
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
            _ai_with_tool_call("code_search", {"query": "q4"}, "tc4"),
            _tool_msg("tc4", "code_search", _result(4)),
        ]
        messages = add_messages([], raw)
        before_count = len(messages)

        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())

        after = add_messages(messages, result["messages"])
        assert len(after) == before_count  # no duplication
        tc1 = next(m for m in after if isinstance(m, ToolMessage) and m.tool_call_id == "tc1")
        assert "BULK1" not in str(tc1.content)  # bulk dropped, content swapped
        assert "[已归档·可重取]" in str(tc1.content)
        # recent 3 untouched: full bulk still present
        for tc_id, n in (("tc2", 2), ("tc3", 3), ("tc4", 4)):
            m = next(x for x in after if isinstance(x, ToolMessage) and x.tool_call_id == tc_id)
            assert f"BULK{n}" in str(m.content)

    async def test_no_elision_when_under_keep_recent(self) -> None:
        raw = [
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())
        assert result is None  # 3 tool msgs, keep_recent=3 -> nothing to elide

    async def test_disabled_returns_none(self) -> None:
        raw = [
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = False
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())
        assert result is None

    async def test_does_not_touch_non_tool_messages(self) -> None:
        """Only ToolMessages are elided; AIMessage tool_calls / System / Human
        are never in the replacement set."""
        raw = [
            SystemMessage(content="sys"),
            HumanMessage(content="evidence"),
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
            _ai_with_tool_call("code_search", {"query": "q4"}, "tc4"),
            _tool_msg("tc4", "code_search", _result(4)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())
        assert result is not None
        for repl in result["messages"]:
            assert isinstance(repl, ToolMessage)

    async def test_empty_state_returns_none(self) -> None:
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            assert await mw.abefore_model(state={"messages": []}, runtime=_make_runtime()) is None
            assert await mw.abefore_model(state={}, runtime=_make_runtime()) is None

    async def test_preceding_aimessage_tool_call_still_associates(self) -> None:
        """Regression: the AIMessage that issued tc1 must still find the replaced
        ToolMessage by tool_call_id (structure not broken by elision)."""
        raw = [
            _ai_with_tool_call("search_observability", {"source": "loki", "query": "q1"}, "tc1"),
            _tool_msg("tc1", "search_observability", _obs_result()),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
            _ai_with_tool_call("code_search", {"query": "q4"}, "tc4"),
            _tool_msg("tc4", "code_search", _result(4)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            result = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime())
        after = add_messages(messages, result["messages"])
        # tc1 ToolMessage still present (replaced in place) with matching tool_call_id
        tc1_msgs = [m for m in after if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"]
        assert len(tc1_msgs) == 1
        # the AIMessage with tool_calls=[...tc1...] is unchanged
        ai_with_tc1 = [
            m
            for m in after
            if isinstance(m, AIMessage)
            and any(tc.get("id") == "tc1" for tc in (m.tool_calls or []))
        ]
        assert len(ai_with_tc1) == 1

    async def test_records_elided_tool_call_ids_for_dedup_refetch(self) -> None:
        """§7.x contract: abefore_model records each aged tool_call_id into
        ctx.elided_tool_call_ids so ToolDedupMiddleware can allow re-fetches
        of elided results instead of skipping them (the elision <-> dedup
        contract documented on DiagnosisRunContext.elided_tool_call_ids)."""
        ctx = DiagnosisRunContext(case_id="TEST-ELISION")
        raw = [
            _ai_with_tool_call("code_search", {"query": "q1"}, "tc1"),
            _tool_msg("tc1", "code_search", _result(1)),
            _ai_with_tool_call("code_search", {"query": "q2"}, "tc2"),
            _tool_msg("tc2", "code_search", _result(2)),
            _ai_with_tool_call("code_search", {"query": "q3"}, "tc3"),
            _tool_msg("tc3", "code_search", _result(3)),
            _ai_with_tool_call("code_search", {"query": "q4"}, "tc4"),
            _tool_msg("tc4", "code_search", _result(4)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime(ctx))
        # tc1 is the only one aged out (keep_recent=3); tc2-tc4 retained
        assert "tc1" in ctx.elided_tool_call_ids
        assert "tc2" not in ctx.elided_tool_call_ids
        assert "tc3" not in ctx.elided_tool_call_ids

    async def test_second_pass_skips_already_archived_no_degradation(self) -> None:
        """Multi-pass: a ToolMessage archived in pass 1 is skipped in pass 2
        (not re-processed), so its placeholder's key finding is NOT degraded.
        Without the skip, re-running build_elision_placeholder on the placeholder
        content would take the placeholder's first line (the handle) as the
        finding, losing the original key finding."""
        ctx = DiagnosisRunContext(case_id="TEST-MULTIPASS")
        raw = [
            _ai_with_tool_call("get_file_content", {"file_path": "app/svc.py"}, "tc1"),
            _tool_msg("tc1", "get_file_content", "def foo():\n    pass\n"),
            _ai_with_tool_call("get_file_content", {"file_path": "b.py"}, "tc2"),
            _tool_msg("tc2", "get_file_content", _result(2)),
            _ai_with_tool_call("get_file_content", {"file_path": "c.py"}, "tc3"),
            _tool_msg("tc3", "get_file_content", _result(3)),
            _ai_with_tool_call("get_file_content", {"file_path": "d.py"}, "tc4"),
            _tool_msg("tc4", "get_file_content", _result(4)),
        ]
        messages = add_messages([], raw)
        mw = ContextElisionMiddleware()
        with _patch_settings() as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            # pass 1: tc1 aged out (rank 3) -> archived with original finding
            r1 = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime(ctx))
            assert r1 is not None
            messages = add_messages(messages, r1["messages"])
            tc1 = next(
                m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
            )
            assert "def foo():" in str(tc1.content)  # original key finding kept
            assert "tc1" in ctx.elided_tool_call_ids

            # pass 2: same messages -> tc1 already archived -> skipped;
            # tc2-tc4 still within keep_recent -> nothing to do
            r2 = await mw.abefore_model(state={"messages": messages}, runtime=_make_runtime(ctx))
            assert r2 is None  # no new replacements (tc1 skipped, not re-elided)
            # tc1 content unchanged: finding NOT degraded to the handle line
            tc1_after = next(
                m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
            )
            assert "def foo():" in str(tc1_after.content)
            assert "[已归档·可重取]" in str(tc1_after.content)
