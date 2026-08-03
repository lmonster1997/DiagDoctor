"""#5 HITL (interrupt + resume) tests for the CopilotKit diagnosis graph.

Verifies the orchestration that #5 adds - the stuff that ISN'T already covered
by test_checkpointer_reducer.py (reducers + sqlite persistence) or
test_middleware.py (BudgetGuard -> jump_to='end'):

- budget exhaustion routes to the ``human_input`` interrupt node (pause)
- ``Command(resume=<guidance>)`` resumes -> informed second pass -> convergence
- normal completion (no exhaustion) skips HITL entirely
- one-shot gate: a second exhaustion after resume routes to END (no loop)
- a paused diagnosis survives a fresh graph+saver instance (cross-process resume)

The inner ``create_agent`` ReAct loop is replaced by a ``_FakeAgent`` so the
test is deterministic and exercises the REAL outer graph + REAL
``_diagnosis_agent_node`` / ``human_input_node`` / routing / checkpoint logic.
The fake still drives the real ``update_budget`` / ``parse_diagnosis_report``
code paths - pass 1 simulates budget exhaustion by setting
``context.budget_exhausted = True`` (the real BudgetGuard would do this on the
iteration cap; the fake bypasses middlewares, so it sets the flag directly on
the run context the node reads back) and returns 17 flailing tool-call messages
(no JSON), pass 2 returns a converged JSON report. Only the LLM is faked; the
HITL wiring is the real thing.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from src.engine.checkpointer import _LazyAsyncSqliteSaver
from src.engine.nodes import diagnosis_agent as diag_mod
from src.engine.state import Evidence

# A valid DiagnosisReport JSON the fake agent emits on the converged pass.
CONVERGED_JSON = """```json
{
  "primary_category": "logic",
  "categories": ["logic"],
  "symptom_tier": "backend",
  "root_cause_tier": "backend",
  "root_cause": "comment_service.update_comment 未校验 owner 导致 IDOR",
  "affected_file": "app/services/comment_service.py",
  "affected_function": "update_comment",
  "fix_suggestion": "【文件】comment_service.py\\n【位置】update_comment\\n【改后】加 owner 校验",
  "evidence_chain": ["sig-1"],
  "confidence": 0.85
}
```"""

_RESUME_MARKER = "续查模式"


class _FakeAgent:
    """Replaces the inner create_agent. Branches on the resume marker.

    - resume (continuation HumanMessage present): emit converged JSON report.
    - else (pass 1): flail - 17 tool-call AIMessages, no JSON, and set
      ``context.budget_exhausted = True`` (faking what BudgetGuard does on the
      iteration cap) -> ``early_stopped`` -> route to human_input.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        is_resume = any(_RESUME_MARKER in str(getattr(m, "content", "")) for m in msgs)
        if is_resume:
            return {"messages": [AIMessage(content=CONVERGED_JSON)]}
        # Pass 1: simulate budget exhaustion. The real BudgetGuardMiddleware
        # would set ctx.budget_exhausted=True on the iteration cap; the fake
        # bypasses middlewares, so set it directly on the run context the node
        # reads back (``context`` is the same run_ctx object the node passed in).
        if context is not None:
            context.budget_exhausted = True
        flail = [
            AIMessage(
                content="",
                tool_calls=[{"name": "fake_tool", "args": {}, "id": f"tc{i}"}],
            )
            for i in range(17)
        ]
        return {"messages": flail}


class _ConvergingFake:
    """Always converges on pass 1 (no budget exhaustion) -> skips HITL."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        return {"messages": [AIMessage(content=CONVERGED_JSON)]}


# ── P1 active-clarification fakes ───────────────────────────────────
# The inner create_agent is faked, so ClarificationMiddleware never runs. The
# fakes set ``context.clarification_requested`` + ``clarification_question``
# directly (mirroring what the middleware does on the real loop after seeing a
# ``request_user_clarification`` tool_call). The outer-graph clarify_input
# interrupt + routing is the REAL code under test.

_CLARIFY_RESUME_MARKER = "【澄清续查】"  # emitted by _diagnosis_agent_node on a clarify resume
_CLARIFY_QUESTION = "这个报错是偶发还是必现?"


def _clarify_msg(question: str, tc_id: str) -> AIMessage:
    """An AIMessage carrying a request_user_clarification tool_call (what the
    real agent emits when it proactively asks the user)."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "request_user_clarification", "args": {"question": question}, "id": tc_id}],
    )


class _ClarifyingFake:
    """Pass 1: agent asks a clarification question. Pass 2 (clarify resume): converge.

    Mirrors ``_FakeAgent``'s branching: pass 1 sets the run-context flag the
    real ClarificationMiddleware would set, plus emits the clarify tool_call
    AIMessage; the clarify-resume pass (detected via the ``【澄清续查】`` marker
    the node injects) returns the converged JSON report.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        is_clarify_resume = any(
            _CLARIFY_RESUME_MARKER in str(getattr(m, "content", "")) for m in msgs
        )
        if is_clarify_resume:
            return {"messages": [AIMessage(content=CONVERGED_JSON)]}
        if context is not None:
            context.clarification_requested = True
            context.clarification_question = _CLARIFY_QUESTION
        return {"messages": [_clarify_msg(_CLARIFY_QUESTION, "cc0")]}


class _AlwaysClarifyingFake:
    """Always asks a clarification question, never converges -> exercises the bound."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        if context is not None:
            context.clarification_requested = True
            context.clarification_question = _CLARIFY_QUESTION
        return {"messages": [_clarify_msg(_CLARIFY_QUESTION, f"cc{self.calls}")]}


class _ClarifyThenBudgetFake:
    """Pass 1: ask clarify. Pass 2 (clarify resume): exhaust budget. Pass 3 (budget resume): converge.

    For the coexistence test: active clarification + budget-exhaustion HITL in
    one diagnosis (clarify -> answer -> budget-HITL -> guidance -> converge).
    """

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        content = " ".join(str(getattr(m, "content", "")) for m in msgs)
        if _CLARIFY_RESUME_MARKER in content:
            # Clarify resume pass: exhaust budget -> early_stopped -> human_input.
            if context is not None:
                context.budget_exhausted = True
            flail = [
                AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": f"t{i}"}])
                for i in range(17)
            ]
            return {"messages": flail}
        if _RESUME_MARKER in content:
            # Budget-HITL resume pass: converge.
            return {"messages": [AIMessage(content=CONVERGED_JSON)]}
        # Pass 1: ask clarify.
        if context is not None:
            context.clarification_requested = True
            context.clarification_question = _CLARIFY_QUESTION
        return {"messages": [_clarify_msg(_CLARIFY_QUESTION, "cc0")]}


def _initial_state(thread_id: str) -> dict[str, Any]:
    """REST-path initial state: raw_evidence, no trigger_time (skips prefetch)."""
    return {
        "raw_evidence": Evidence(user_report="comments 接口能改别人的评论(IDOR)"),
        "case_id": thread_id,
        "trace_id": thread_id,
        "session_id": thread_id,
    }


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> _FakeAgent:
    fake = _FakeAgent()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    return fake


@pytest.fixture
def clarify_fake(monkeypatch: pytest.MonkeyPatch) -> _ClarifyingFake:
    fake = _ClarifyingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    return fake


def _build(tmp_path: pytest.TempPathFactory, db_name: str = "cp.db") -> Any:
    saver = _LazyAsyncSqliteSaver(str(tmp_path / db_name))
    return diag_mod.build_copilotkit_graph(checkpointer=saver)


# ── case_id ownership (bug_info_node derives it from config.thread_id) ──


async def test_bug_info_sets_case_id_from_config_thread_id(
    tmp_path: pytest.Path, fake_agent: _FakeAgent
) -> None:
    """bug_info_node (entry) owns case_id: derives it from config.thread_id
    when the caller didn't supply one.

    This is what lets the CopilotKit path -- which never injects case_id --
    still expose ``state.case_id == checkpoint thread_id`` to the frontend /
    👍 feedback loop. Without it state.case_id is None and feedback falls
    back to a desynced hook id (404). case_id == thread_id by construction:
    the checkpointer addresses the checkpoint by the same
    config.configurable.thread_id (see bug_info.py).
    """
    graph = _build(tmp_path)
    tid = "caseid-from-config-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}
    # Initial state WITHOUT case_id/trace_id/session_id (mimics the CopilotKit
    # path, where nobody injects them). raw_evidence -> REST branch skips LLM.
    state = {"raw_evidence": Evidence(user_report="comments 接口能改别人的评论(IDOR)")}

    await graph.ainvoke(state, cfg)

    vals = (await graph.aget_state(cfg)).values or {}
    assert vals.get("case_id") == tid
    assert vals.get("trace_id") == tid
    assert vals.get("session_id") == tid


# ── pause ────────────────────────────────────────────────────────────


async def test_budget_exhaustion_pauses_for_human_input(
    tmp_path: pytest.Path, fake_agent: _FakeAgent
) -> None:
    graph = _build(tmp_path)
    tid = "hitl-pause-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)

    snap = await graph.aget_state(cfg)
    assert "human_input" in snap.next, f"expected pause at human_input, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("early_stopped") is True
    # No HITL cycle has run yet.
    assert not vals.get("hitl_resumed")
    # The interrupt payload is reachable on the pending task.
    assert snap.tasks, "expected a pending (interrupted) task"
    interrupts = getattr(snap.tasks[0], "interrupts", None)
    assert interrupts, "expected an interrupt on the pending task"
    assert interrupts[0].value["type"] == "hitl_guidance_request"
    assert fake_agent.calls == 1


# ── resume ───────────────────────────────────────────────────────────


async def test_resume_with_guidance_converges(
    tmp_path: pytest.Path, fake_agent: _FakeAgent
) -> None:
    graph = _build(tmp_path)
    tid = "hitl-resume-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}
    guidance = "检查 comment_service.update_comment 的 owner 校验"

    # Pass 1 -> pause.
    await graph.ainvoke(_initial_state(tid), cfg)
    assert "human_input" in (await graph.aget_state(cfg)).next

    # Resume with guidance -> pass 2 (informed second pass) -> converge.
    await graph.ainvoke(Command(resume=guidance), cfg)

    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("hitl_resumed") is True
    assert vals.get("human_guidance") == guidance
    report = vals.get("report")
    assert report is not None
    assert report.early_stopped is False
    assert report.primary_category == "logic"
    assert report.confidence == pytest.approx(0.85)
    assert fake_agent.calls == 2  # pass 1 + pass 2
    # add_messages: visible messages from BOTH passes persist in state.
    messages = vals.get("messages", [])
    assert len(messages) >= 16, f"expected pass1+pass2 messages accumulated, got {len(messages)}"


async def test_resume_empty_guidance_accepts_current(
    tmp_path: pytest.Path, fake_agent: _FakeAgent
) -> None:
    """Empty guidance = operator declines to steer -> accept current report -> END."""
    graph = _build(tmp_path)
    tid = "hitl-empty-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)
    assert "human_input" in (await graph.aget_state(cfg)).next

    await graph.ainvoke(Command(resume=""), cfg)

    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("hitl_resumed") is True
    assert vals.get("human_guidance") is None
    # No second pass (empty guidance routes human_input -> END directly).
    assert fake_agent.calls == 1
    # The early_stopped best-effort report is kept.
    assert vals.get("early_stopped") is True


# ── skip HITL on normal completion ───────────────────────────────────


async def test_normal_completion_skips_hitl(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _ConvergingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    tid = "hitl-normal-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)

    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END (no HITL), next={snap.next}"
    vals = snap.values or {}
    assert vals.get("early_stopped") is False
    report = vals.get("report")
    assert report is not None and report.primary_category == "logic"
    assert fake.calls == 1


# ── one-shot gate (no infinite HITL loop) ────────────────────────────


async def test_one_shot_hitl_no_loop(tmp_path: pytest.Path, fake_agent: _FakeAgent) -> None:
    """Pass 2 also exhausts budget -> END (hitl_resumed gates a second pause)."""

    # Override ainvoke to flail on BOTH passes (second exhaustion on resume).
    async def always_flail(
        state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        fake_agent.calls += 1
        # Both passes exhaust budget (fake bypasses BudgetGuard -> set flag).
        if context is not None:
            context.budget_exhausted = True
        flail = [
            AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": f"t{i}"}])
            for i in range(17)
        ]
        return {"messages": flail}

    fake_agent.ainvoke = always_flail  # type: ignore[assignment]

    graph = _build(tmp_path)
    tid = "hitl-oneshot-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)
    assert "human_input" in (await graph.aget_state(cfg)).next

    await graph.ainvoke(Command(resume="再查一次"), cfg)

    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END (one-shot), next={snap.next}"
    vals = snap.values or {}
    assert vals.get("hitl_resumed") is True
    # Second pass also exhausted -> early_stopped, but NOT re-paused.
    assert vals.get("early_stopped") is True
    assert fake_agent.calls == 2


# ── cross-process resume (fresh graph + saver, same db + thread_id) ──


async def test_resume_survives_fresh_graph(tmp_path: pytest.Path, fake_agent: _FakeAgent) -> None:
    """A paused diagnosis resumes from a fresh graph+saver on the same db file.

    Mirrors test_checkpointer_reducer's persistence test: the checkpoint must
    survive losing the in-memory graph instance (process restart equivalent).
    """
    db = str(tmp_path / "cp.db")

    graph_a = diag_mod.build_copilotkit_graph(checkpointer=_LazyAsyncSqliteSaver(db))
    tid = "hitl-crossproc-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}
    await graph_ainvoke_pause(graph_a, tid)

    # Fresh graph + fresh saver, SAME db file + thread_id.
    graph_b = diag_mod.build_copilotkit_graph(checkpointer=_LazyAsyncSqliteSaver(db))
    snap = await graph_b.aget_state(cfg)
    assert "human_input" in snap.next, "paused state must survive fresh graph instance"

    await graph_b.ainvoke(Command(resume="跨进程续查引导"), cfg)

    snap2 = await graph_b.aget_state(cfg)
    assert snap2.next == (), f"expected END after cross-process resume, next={snap2.next}"
    vals = snap2.values or {}
    assert vals.get("hitl_resumed") is True
    assert vals.get("human_guidance") == "跨进程续查引导"
    report = vals.get("report")
    assert report is not None and report.early_stopped is False
    assert fake_agent.calls == 2


async def graph_ainvoke_pause(graph: Any, tid: str) -> None:
    await graph.ainvoke(_initial_state(tid), {"configurable": {"thread_id": tid}})


# ── P1 active clarification (agent proactively asks the user) ─────────


async def test_active_clarification_pauses_and_resumes(
    tmp_path: pytest.Path, clarify_fake: _ClarifyingFake
) -> None:
    """Agent proactively asks -> pause at clarify_input -> resume -> converge."""
    graph = _build(tmp_path)
    tid = "clarify-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}
    answer = "偶发,大约每天一次"

    # Pass 1 -> agent asks -> pause at clarify_input.
    await graph.ainvoke(_initial_state(tid), cfg)

    snap = await graph.aget_state(cfg)
    assert "clarify_input" in snap.next, f"expected pause at clarify_input, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("clarification_requested") is True
    assert vals.get("clarification_question") == _CLARIFY_QUESTION
    assert snap.tasks, "expected a pending (interrupted) task"
    interrupts = getattr(snap.tasks[0], "interrupts", None)
    assert interrupts, "expected an interrupt on the pending task"
    assert interrupts[0].value["type"] == "clarify"
    assert interrupts[0].value["question"] == _CLARIFY_QUESTION
    assert clarify_fake.calls == 1

    # Resume with a non-empty answer -> pass 2 (clarification续查) -> converge.
    await graph.ainvoke(Command(resume=answer), cfg)

    snap2 = await graph.aget_state(cfg)
    assert snap2.next == (), f"expected END, next={snap2.next}"
    vals2 = snap2.values or {}
    assert vals2.get("clarification_count") == 1
    # clarification_answer is cleared after the consuming pass (anti-stale).
    assert vals2.get("clarification_answer") is None
    report = vals2.get("report")
    assert report is not None and report.primary_category == "logic"
    assert report.early_stopped is False
    assert clarify_fake.calls == 2


async def test_clarification_empty_answer_accepts_current(
    tmp_path: pytest.Path, clarify_fake: _ClarifyingFake
) -> None:
    """Empty answer = operator declines -> accept current best-effort -> END."""
    graph = _build(tmp_path)
    tid = "clarify-empty-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await graph.ainvoke(_initial_state(tid), cfg)
    assert "clarify_input" in (await graph.aget_state(cfg)).next

    await graph.ainvoke(Command(resume=""), cfg)

    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("clarification_count") == 1
    # No second pass (empty answer routes clarify_input -> END directly).
    assert clarify_fake.calls == 1


async def test_clarification_bounded(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking more than MAX_CLARIFICATIONS times is bounded -> END, no infinite loop."""
    fake = _AlwaysClarifyingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    tid = "clarify-bound-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    # Pass 1: ask (count 0 < MAX) -> clarify -> count 1.
    await graph.ainvoke(_initial_state(tid), cfg)
    assert "clarify_input" in (await graph.aget_state(cfg)).next
    await graph.ainvoke(Command(resume="回答1"), cfg)

    # Pass 2: ask again (count 1 < MAX) -> clarify -> count 2.
    assert "clarify_input" in (await graph.aget_state(cfg)).next
    await graph.ainvoke(Command(resume="回答2"), cfg)

    # Pass 3: ask again (count 2, NOT < MAX=2) -> bound hit -> END (ask dropped).
    snap = await graph.aget_state(cfg)
    assert snap.next == (), f"expected END after bound, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("clarification_count") == 2
    assert fake.calls == 3  # pass 1 + pass 2 + pass 3 (3rd ask ignored)


async def test_clarification_coexists_with_budget_hitl(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clarify -> answer -> budget exhausts -> human_input -> guidance -> converge.

    Active clarification and #5 budget-exhaustion HITL coexist: clarification
    has its own bounded gate (clarification_count), independent of the budget
    HITL one-shot gate (hitl_resumed). Neither precludes the other.
    """
    fake = _ClarifyThenBudgetFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    tid = "clarify-coexist-1"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    # Pass 1: agent asks clarify -> pause at clarify_input.
    await graph.ainvoke(_initial_state(tid), cfg)
    assert "clarify_input" in (await graph.aget_state(cfg)).next

    # Resume clarify answer -> pass 2 exhausts budget -> pause at human_input.
    await graph.ainvoke(Command(resume="偶发"), cfg)
    snap = await graph.aget_state(cfg)
    assert "human_input" in snap.next, f"expected budget HITL after clarify, next={snap.next}"
    vals = snap.values or {}
    assert vals.get("clarification_count") == 1
    assert vals.get("early_stopped") is True
    assert not vals.get("hitl_resumed")

    # Resume budget guidance -> pass 3 converges -> END.
    await graph.ainvoke(Command(resume="查 owner 校验"), cfg)
    snap2 = await graph.aget_state(cfg)
    assert snap2.next == (), f"expected END, next={snap2.next}"
    vals2 = snap2.values or {}
    assert vals2.get("hitl_resumed") is True
    report = vals2.get("report")
    assert report is not None and report.primary_category == "logic"
    assert fake.calls == 3


# ── REST endpoints (POST /diagnose, /diagnose/resume, GET /diagnose/threads) ──


def test_rest_pause_list_resume_cycle(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end REST: diagnose (pauses) -> list (paused) -> resume -> list (completed).

    Uses a minimal FastAPI app with only the diagnose router (no lifespan /
    no copilotkit mount) and a monkeypatched graph, so the HTTP wiring of the
    #5 endpoints is exercised without real LLM or services.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import diagnose as diag_api

    fake = _FakeAgent()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    monkeypatch.setattr(diag_api, "get_copilotkit_graph", lambda: graph)

    app = FastAPI()
    app.include_router(diag_api.router)
    # Use TestClient as a context manager so all requests share one event loop
    # (matches uvicorn's single loop) -- the _LazyAsyncSqliteSaver binds its
    # aiosqlite connection to the loop it first materialises on.
    with TestClient(app) as client:
        # 1. Start diagnosis -> budget exhausts -> pauses at human_input.
        r = client.post("/api/diagnose", json={"evidence": {"user_report": "comments IDOR"}})
        assert r.status_code == 200, r.text
        body = r.json()
        tid = body["thread_id"]
        assert body["report"]["early_stopped"] is True  # best-effort report before pause

        # 2. List threads -> the paused thread shows up first.
        r = client.get("/api/diagnose/threads")
        assert r.status_code == 200, r.text
        matched = [t for t in r.json()["threads"] if t["thread_id"] == tid]
        assert matched and matched[0]["status"] == "paused"

        # 3. Resume with guidance -> informed second pass -> converged report.
        r = client.post(
            "/api/diagnose/resume", json={"thread_id": tid, "guidance": "查 owner 校验"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report"]["early_stopped"] is False
        assert body["report"]["primary_category"] == "logic"

        # 4. List threads -> now completed.
        r = client.get("/api/diagnose/threads")
        matched = [t for t in r.json()["threads"] if t["thread_id"] == tid]
        assert matched and matched[0]["status"] == "completed"

        # 5. Resume a completed (non-paused) thread -> 409.
        r = client.post("/api/diagnose/resume", json={"thread_id": tid, "guidance": "x"})
        assert r.status_code == 409


# ── REST GET /diagnose/threads/{tid} (P0: view historical report) ─────


def test_rest_get_thread_detail_completed(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/diagnose/threads/{tid} returns the full report of a completed
    thread, and 404s for an unknown thread_id (P0 historical report view)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import diagnose as diag_api

    fake = _ConvergingFake()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    monkeypatch.setattr(diag_api, "get_copilotkit_graph", lambda: graph)

    app = FastAPI()
    app.include_router(diag_api.router)
    with TestClient(app) as client:
        # 1. Run a converging diagnosis -> completed.
        r = client.post("/api/diagnose", json={"evidence": {"user_report": "comments IDOR"}})
        assert r.status_code == 200, r.text
        tid = r.json()["thread_id"]

        # 2. GET the thread detail -> full report payload (same shape as POST).
        r = client.get(f"/api/diagnose/threads/{tid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["thread_id"] == tid
        assert body["report"] is not None
        assert body["report"]["primary_category"] == "logic"
        assert body["report"]["early_stopped"] is False
        assert "findings_count" in body

        # 3. Unknown thread_id -> 404.
        r = client.get("/api/diagnose/threads/never-existed")
        assert r.status_code == 404


def test_rest_get_thread_detail_paused(
    tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/diagnose/threads/{tid} on a paused thread returns the
    best-effort early_stopped report (so an operator can read it before
    deciding whether to resume)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import diagnose as diag_api

    fake = _FakeAgent()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: fake)
    graph = _build(tmp_path)
    monkeypatch.setattr(diag_api, "get_copilotkit_graph", lambda: graph)

    app = FastAPI()
    app.include_router(diag_api.router)
    with TestClient(app) as client:
        # 1. Run -> budget exhausts -> pauses at human_input.
        r = client.post("/api/diagnose", json={"evidence": {"user_report": "comments IDOR"}})
        assert r.status_code == 200, r.text
        tid = r.json()["thread_id"]

        # 2. GET the paused thread -> early_stopped report is readable.
        r = client.get(f"/api/diagnose/threads/{tid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report"] is not None
        assert body["report"]["early_stopped"] is True
