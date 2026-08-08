"""P2 spike tests: confirm the mechanism behind "fresh start after END".

S2 (docs/hitl-evolution-plan.md §5.1): empirically confirm what happens when an
already-ENDED diagnosis thread is re-invoked. The doc's §0.4 records a
disagreement about *why* a follow-up message after END starts fresh:

  - "get_state returns threadExists:False" (the dead-code hypothesis)
  - "LangGraph naturally restarts an ENDed thread from the entry node" (the
    prepare_stream / LangGraph hypothesis)

S1 (grep + call-chain) already settled that ``DiagDoctorAgent.get_state`` is
dead code -- nothing in copilotkit / ag_ui_langgraph / our own code calls it.
These tests settled S2: they re-invoke an ENDed thread directly via
``graph.ainvoke`` (the REST path, no ``prepare_stream`` involvement) and assert
the graph re-runs from ``bug_info`` rather than erroring or no-opping. They
also guard the P2 fix for the round-scoped state bleed (persisted
``hitl_resumed`` / ``clarification_count`` / ``early_stopped`` + ``findings``
accumulation via the ``add`` reducer): ``bug_info_node`` now resets these on a
follow-up round (see ``test_followup_round_resets_round_scoped_flags``).

Only the LLM is faked (``_ConvergingFake``); the outer graph, bug_info,
diagnosis_agent, routing and checkpointer are the real code.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from src.engine.nodes import diagnosis_agent as diag_mod
from src.engine.state import Evidence
from tests.graph.test_hitl import _build, _ConvergingFake


def _initial_state(thread_id: str, user_report: str = "comments IDOR") -> dict[str, Any]:
    return {
        "raw_evidence": Evidence(user_report=user_report),
        "case_id": thread_id,
        "trace_id": thread_id,
        "session_id": thread_id,
    }


async def test_ended_thread_rerun_from_bug_info(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2: re-invoking an ENDed thread re-runs from bug_info (fresh diagnosis).

    The "fresh start after END" phenomenon's mechanism is LangGraph's natural
    behaviour for a thread with ``next == ()`` (no pending node -> restart from
    the entry point), NOT ``get_state`` (dead code, S1). Confirmed by:
      - diagnosis_agent re-runs (fake.calls 1 -> 2)
      - bug_info re-runs with the new input (evidence.user_report changes)
      - no exception, re-converges to END (next == ())
    """
    fake = _ConvergingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    tid = "p2-spike-rerun-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    # Round 1 -> converge -> END.
    await graph.ainvoke(_initial_state(tid, "comments IDOR"), cfg)
    snap1 = await graph.aget_state(cfg)
    assert snap1.next == (), f"expected END after round 1, next={snap1.next}"
    vals1 = snap1.values or {}
    assert fake.calls == 1
    evidence1 = vals1.get("evidence")
    assert evidence1 is not None
    assert evidence1.user_report == "comments IDOR"

    # Round 2: re-invoke the SAME (ENDed) thread with new evidence -- mirrors a
    # user sending a follow-up after the diagnosis ended.
    await graph.ainvoke(_initial_state(tid, "追加:其实是 cache 失效"), cfg)
    snap2 = await graph.aget_state(cfg)
    vals2 = snap2.values or {}

    # S2 core: the graph re-ran from bug_info (fresh), it did not error / no-op.
    assert fake.calls == 2, "ENDed thread re-invoke must re-run diagnosis_agent"
    evidence2 = vals2.get("evidence")
    assert evidence2 is not None
    assert evidence2.user_report == "追加:其实是 cache 失效", "bug_info re-ran with new input"
    assert snap2.next == (), f"expected END after round 2, next={snap2.next}"


async def test_followup_round_resets_round_scoped_flags(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 fix: round-scoped flags are RESET at the start of a follow-up round.

    S2 (spike) found that ``hitl_resumed`` / ``clarification_count`` /
    ``early_stopped`` persisted across rounds (bug_info never cleared them) -- so
    a round-1 budget-HITL diagnosis left ``hitl_resumed=True``, which would make
    a round-2 budget exhaustion skip HITL entirely (one-shot gate already
    tripped). P2 fixes this in ``bug_info_node``: on a follow-up round (prior
    ``report`` exists) it resets the round-scoped flags so the follow-up round
    gets a fresh HITL/clarification budget. This test guards that fix.
    """
    from langgraph.types import Command

    from tests.graph.test_hitl import _FakeAgent

    # Round 1: budget exhausts -> human_input -> resume with guidance -> converge -> END.
    fake = _FakeAgent()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    tid = "p2-spike-bleed-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)
    assert "human_input" in (await graph.aget_state(cfg)).next
    await graph.ainvoke(Command(resume="查 owner 校验"), cfg)
    snap1 = await graph.aget_state(cfg)
    assert snap1.next == (), "round 1 ENDed after resume"
    vals1 = snap1.values or {}
    assert vals1.get("hitl_resumed") is True
    assert vals1.get("round") == 1

    # Round 2: re-invoke the ENDed thread -> bug_info detects follow-up round.
    conv = _ConvergingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: conv)
    await graph.ainvoke(_initial_state(tid, "追加信息"), cfg)
    vals2 = (await graph.aget_state(cfg)).values or {}

    # THE FIX: round bumped to 2, and round-scoped flags RESET (not bled).
    assert vals2.get("round") == 2, "round bumped on follow-up"
    assert vals2.get("hitl_resumed") is False, "hitl_resumed reset on follow-up (was True in round 1)"
    assert vals2.get("clarification_count") == 0, "clarification_count reset on follow-up"
    # early_stopped overwritten by the converging pass anyway; the reset matters
    # for a budget-exhausting follow-up (would re-enable HITL, tested in test_hitl).
    assert conv.calls == 1


async def test_ended_thread_reinvoke_via_messages_path(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2: the CopilotKit chat path (messages, not raw_evidence) also re-runs.

    bug_info's CopilotKit branch reads ``messages[-1]`` as the user message and
    calls ``_extract_bug_info`` (LLM). We monkeypatch the extractor to stay
    deterministic, then confirm a follow-up HumanMessage on an ENDed thread
    re-runs bug_info + diagnosis_agent -- i.e. the chat UI's "fresh start after
    END" is the same LangGraph mechanism, not a get_state decision.
    """
    fake = _ConvergingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)

    extracted: list[str] = []

    async def _fake_extract(user_message: str) -> tuple[dict[str, Any], list[Any]]:
        extracted.append(user_message)
        return {"bug_description": user_message, "trigger_time": None, "trace_ids": []}, []

    # bug_info_node looks up _extract_bug_info in its own module globals at call
    # time, so patch the bug_info module attribute directly.
    import src.engine.nodes.bug_info as bug_mod

    monkeypatch.setattr(bug_mod, "_extract_bug_info", _fake_extract)

    graph = _build(tmp_path)
    tid = "p2-spike-msg-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    # Round 1 via messages path (CopilotKit-style).
    await graph.ainvoke({"messages": [HumanMessage(content="首页白屏")]}, cfg)
    snap1 = await graph.aget_state(cfg)
    assert snap1.next == ()
    assert fake.calls == 1
    assert extracted == ["首页白屏"]

    # Round 2: follow-up message on the ENDed thread.
    await graph.ainvoke({"messages": [HumanMessage(content="偶发,每天一次")]}, cfg)
    snap2 = await graph.aget_state(cfg)
    vals2 = snap2.values or {}

    assert fake.calls == 2, "chat path: ENDed thread re-invoke re-runs diagnosis_agent"
    assert extracted == ["首页白屏", "偶发,每天一次"], "bug_info re-ran on the new message"
    assert snap2.next == ()
    evidence2 = vals2.get("evidence")
    assert evidence2 is not None
    assert evidence2.user_report == "偶发,每天一次"
