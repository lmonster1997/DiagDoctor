"""Tests for #7: typed DoctorState reducers + persistent SQLite checkpointer.

Covers the two halves of followup-plan #7:
1. **Real reducers**: ``StateGraph(DoctorState)`` (TypedDict) makes the declared
   ``add`` reducers on ``findings`` / ``hypotheses`` / ``budget_ticks`` /
   ``total_cost`` actually accumulate across nodes (the old ``StateGraph(dict)``
   declared them but they were dead -- node returns did dict-overwrite).
   ``messages`` uses ``add_messages`` (#5 HITL-resume: the chat history
   accumulates across the pause/resume boundary so pass-1 reasoning survives
   into the resumed thread).
2. **Persistent checkpointer**: ``_LazyAsyncSqliteSaver`` materialises an
   ``AsyncSqliteSaver`` on first use and persists checkpoints to SQLite, so a
   fresh graph+saver reading the same db file + thread_id recovers prior state
   (the foundation for #5 ``interrupt()`` + resume).

These use a tiny 2-node graph built on the real ``DoctorState`` schema + the
real ``_LazyAsyncSqliteSaver`` (no LLM, no services) to isolate the mechanism.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from src.engine.checkpointer import _LazyAsyncSqliteSaver
from src.engine.nodes.diagnosis_agent import build_copilotkit_graph
from src.engine.state import DoctorState, Finding

# ═════════════════════════════════════════════════════════════════════
# Helpers: a minimal 2-node graph on DoctorState (no LLM)
# ═════════════════════════════════════════════════════════════════════


async def _node_a(state: DoctorState) -> dict[str, Any]:
    """First node: writes findings + total_cost (add) and messages (add_messages)."""
    return {
        "findings": [Finding(summary="f1", confidence=0.5)],
        "total_cost": 0.5,
        "messages": [AIMessage(content="msg-a")],
    }


async def _node_b(state: DoctorState) -> dict[str, Any]:
    """Second node: appends findings + total_cost; appends messages (add_messages)."""
    return {
        "findings": [Finding(summary="f2", confidence=0.7)],
        "total_cost": 0.3,
        "messages": [AIMessage(content="msg-b")],
    }


def _build_test_graph(saver: _LazyAsyncSqliteSaver) -> Any:
    """Build a 2-node graph (a -> b) on DoctorState with the given checkpointer."""
    builder = StateGraph(DoctorState)
    builder.add_node("a", _node_a)
    builder.add_node("b", _node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=saver)


# ═════════════════════════════════════════════════════════════════════
# Reducers
# ═════════════════════════════════════════════════════════════════════


class TestReducersRun:
    """The declared ``add`` reducers must actually run under StateGraph(DoctorState)."""

    async def test_findings_accumulate_across_nodes(self, tmp_path: Any) -> None:
        saver = _LazyAsyncSqliteSaver(str(tmp_path / "ck.db"))
        graph = _build_test_graph(saver)
        result = await graph.ainvoke({}, {"configurable": {"thread_id": "t1"}})

        findings: list[Finding] = result["findings"]
        summaries = [f.summary for f in findings]
        assert summaries == ["f1", "f2"]  # add reducer -> accumulated, not overwritten

    async def test_total_cost_accumulates(self, tmp_path: Any) -> None:
        saver = _LazyAsyncSqliteSaver(str(tmp_path / "ck.db"))
        graph = _build_test_graph(saver)
        result = await graph.ainvoke({}, {"configurable": {"thread_id": "t1"}})

        assert result["total_cost"] == pytest.approx(0.8)  # 0.5 + 0.3

    async def test_messages_accumulate_via_add_messages(self, tmp_path: Any) -> None:
        """messages uses add_messages -> node b appends to node a's messages.

        #5 HITL-resume requires the chat history to accumulate across the
        pause/resume boundary (both passes' visible messages persist in the
        synced state) instead of the second pass clobbering the first.
        """
        saver = _LazyAsyncSqliteSaver(str(tmp_path / "ck.db"))
        graph = _build_test_graph(saver)
        result = await graph.ainvoke({}, {"configurable": {"thread_id": "t1"}})

        messages: list[Any] = result["messages"]
        contents = [m.content for m in messages]
        assert contents == ["msg-a", "msg-b"]  # add_messages -> accumulated


# ═════════════════════════════════════════════════════════════════════
# Persistence
# ═════════════════════════════════════════════════════════════════════


class TestSqlitePersistence:
    """Checkpoints survive a fresh graph + saver instance (same db, same thread)."""

    async def test_state_recovers_from_fresh_saver(self, tmp_path: Any) -> None:
        db = str(tmp_path / "persist.db")
        cfg = {"configurable": {"thread_id": "resume-1"}}

        # Run 1: graph + saver A write a checkpoint.
        graph_a = _build_test_graph(_LazyAsyncSqliteSaver(db))
        await graph_a.ainvoke({}, cfg)

        # Run 2: a COMPLETELY FRESH graph + saver B (same db file, same thread_id)
        # recovers the persisted state - no in-memory handoff.
        graph_b = _build_test_graph(_LazyAsyncSqliteSaver(db))
        snapshot = await graph_b.aget_state(cfg)

        assert snapshot.next == ()  # run completed, nothing pending
        values = snapshot.values
        assert [f.summary for f in values["findings"]] == ["f1", "f2"]
        assert values["total_cost"] == pytest.approx(0.8)

    async def test_lazy_saver_materializes_on_first_use(self, tmp_path: Any) -> None:
        """The proxy is constructed sync (no loop); the real saver is created
        only on the first async op, on the running loop."""
        saver = _LazyAsyncSqliteSaver(str(tmp_path / "lazy.db"))
        assert saver._saver is None  # not materialized at construction

        graph = _build_test_graph(saver)
        await graph.ainvoke({}, {"configurable": {"thread_id": "t"}})

        assert saver._saver is not None  # materialized during ainvoke


# ═════════════════════════════════════════════════════════════════════
# Real graph smoke
# ═════════════════════════════════════════════════════════════════════


class TestRealGraphWiring:
    """The production build_copilotkit_graph compiles with DoctorState + lazy saver."""

    def test_builds_with_typed_schema_and_sqlite_saver(self) -> None:
        graph = build_copilotkit_graph()

        # Typed DoctorState schema (not bare dict): LangGraph can introspect it
        # to a JSON schema (CopilotKit's ag-ui path relies on this).
        json_schema = graph.get_input_jsonschema({})
        assert "findings" in json_schema["properties"]
        assert "messages" in json_schema["properties"]
        # Persistent lazy sqlite checkpointer (not in-memory MemorySaver).
        assert isinstance(graph.checkpointer, _LazyAsyncSqliteSaver)
        # Lazy: not materialized at build time (no event loop yet).
        assert graph.checkpointer._saver is None
