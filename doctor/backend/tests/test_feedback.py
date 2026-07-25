"""Tests for the feedback API §8.1 wiring (the "越用越准" loop).

Mocks the LangGraph checkpoint (``get_copilotkit_graph``.aget_state) and the
case_store write-back (``maybe_index_diagnosis`` / ``backfill_effectiveness``).
The fire-and-forget ``asyncio.create_task`` is captured so the test can await
the background coroutine deterministically.

Covers: upvote indexes the new case only (task 3c: no longer backfills recalled
cases -- case-level "有帮助" is the sole effectiveness trigger); 404 when no
report; downvote logs only and never backfills or indexes (design §8.1/§8.2);
case-level POST /{run_id}/case (§8.1 path 2): helpful=True backfills the single
referenced case, helpful=False logs only, 422 when case_id not in
referenced_case_ids (anti-arbitrary-marking), 404 when no report.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from src.api import feedback
from src.engine.nodes import diagnosis_agent
from src.engine.state import DiagnosisReport, NormalizedEvidence, Signal
from src.memory.long_term import case_store

# ── Fixtures / helpers ──────────────────────────────────────────────


def _report(referenced_case_ids: list[str] | None = None) -> DiagnosisReport:
    return DiagnosisReport(
        root_cause="update_comment 未校验 owner",
        affected_file="app/services/comment_service.py",
        fix_suggestion="加 owner 校验",
        confidence=0.85,
        referenced_case_ids=referenced_case_ids or [],
    )


def _evidence() -> NormalizedEvidence:
    return NormalizedEvidence(
        user_report="创建任务后页面卡死",
        golden_signals=[
            Signal(signal_type="error_log", service_tier="backend", summary="TypeError on tags")
        ],
        trigger_time="2026-07-18T10:00:00Z",
        trigger_trace_ids=["t-1"],
    )


def _patch_graph(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> None:
    """Patch ``get_copilotkit_graph`` so ``aget_state`` returns ``values``."""
    snapshot = SimpleNamespace(values=values)
    graph = SimpleNamespace()

    async def fake_aget_state(_config: Any) -> SimpleNamespace:
        return snapshot

    graph.aget_state = fake_aget_state
    monkeypatch.setattr(diagnosis_agent, "get_copilotkit_graph", lambda: graph)


def _patch_store(monkeypatch: pytest.MonkeyPatch, *, index_return: bool = True) -> SimpleNamespace:
    """Patch maybe_index_diagnosis + backfill_effectiveness; record calls."""
    index_calls: list[dict[str, Any]] = []
    backfill_calls: list[dict[str, Any]] = []

    async def fake_index(**kwargs: Any) -> bool:
        index_calls.append(kwargs)
        return index_return

    async def fake_backfill(case_ids: list[str], *, delta: float) -> int:
        backfill_calls.append({"case_ids": list(case_ids), "delta": delta})
        return len(case_ids)

    monkeypatch.setattr(case_store, "maybe_index_diagnosis", fake_index)
    monkeypatch.setattr(case_store, "backfill_effectiveness", fake_backfill)
    return SimpleNamespace(index_calls=index_calls, backfill_calls=backfill_calls)


def _capture_tasks(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture fire-and-forget coroutines instead of scheduling them."""
    coros: list[Any] = []

    def capture(coro: Any) -> SimpleNamespace:
        coros.append(coro)
        return SimpleNamespace()  # handler discards the task handle

    monkeypatch.setattr(asyncio, "create_task", capture)
    return coros


def _state_values(
    *,
    report: DiagnosisReport | None = None,
    retrieved_case_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "report": report if report is not None else _report(),
        "evidence": _evidence(),
        "retrieved_case_ids": retrieved_case_ids if retrieved_case_ids is not None else [],
    }


# ── upvote ─────────────────────────────────────────────────────────


async def test_upvote_indexes_new_case_only_no_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3c: upvote indexes the new case but does NOT backfill recalled cases.

    Case-level "有帮助" (path 2) is the sole effectiveness trigger now; upvote
    only endorses the diagnosis (indexes it). retrieved_case_ids is present in
    state but ignored.
    """
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=["hist-1", "hist-2"]))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    resp = await feedback.upvote("run-1")
    assert resp == {"ok": True, "run_id": "run-1"}
    # fire-and-forget: run the background _index coro
    assert len(coros) == 1
    await coros[0]

    # new case indexed with run_id as point id (idempotency)
    assert len(store.index_calls) == 1
    assert store.index_calls[0]["case_id"] == "run-1"
    assert store.index_calls[0]["trace_id"] == "t-1"
    # task 3c: NO backfill on upvote (coarse attribution removed)
    assert store.backfill_calls == []


async def test_upvote_404_when_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, {"report": None, "evidence": _evidence()})
    _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.upvote("run-1")
    assert exc.value.status_code == 404
    # nothing scheduled before the guard raised
    assert coros == []


# ── downvote ───────────────────────────────────────────────────────


async def test_downvote_logs_only_never_backfills_or_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=["hist-1"]))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    resp = await feedback.downvote("run-1")
    assert resp == {"ok": True, "run_id": "run-1"}
    # 👎 logs only: no background task, no index, no backfill (design §8.1/§8.2)
    assert coros == []
    assert store.index_calls == []
    assert store.backfill_calls == []


async def test_downvote_404_when_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, {"report": None, "evidence": _evidence()})
    _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.downvote("run-1")
    assert exc.value.status_code == 404
    assert coros == []


# ── case-level feedback (§8.1 path 2: POST /{run_id}/case) ─────────


async def test_case_feedback_helpful_backfills_single_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(
        monkeypatch,
        _state_values(report=_report(referenced_case_ids=["hist-1", "hist-2"])),
    )
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    resp = await feedback.case_feedback(
        "run-1", feedback.CaseFeedbackRequest(case_id="hist-2", helpful=True)
    )
    assert resp == {"ok": True, "run_id": "run-1", "case_id": "hist-2", "helpful": True}
    assert len(coros) == 1
    await coros[0]

    # only the marked case is credited (not all referenced, not all retrieved)
    assert len(store.backfill_calls) == 1
    assert store.backfill_calls[0]["case_ids"] == ["hist-2"]
    assert store.backfill_calls[0]["delta"] == pytest.approx(0.1)
    # case-level helpful does NOT index a new case (that's upvote's job)
    assert store.index_calls == []


async def test_case_feedback_not_helpful_no_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(report=_report(referenced_case_ids=["hist-1"])))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    resp = await feedback.case_feedback(
        "run-1", feedback.CaseFeedbackRequest(case_id="hist-1", helpful=False)
    )
    assert resp == {"ok": True, "run_id": "run-1", "case_id": "hist-1", "helpful": False}
    # helpful=False -> no background task, no backfill (只升不降, §8.2)
    assert coros == []
    assert store.backfill_calls == []
    assert store.index_calls == []


async def test_case_feedback_422_when_case_not_referenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(report=_report(referenced_case_ids=["hist-1"])))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.case_feedback(
            "run-1", feedback.CaseFeedbackRequest(case_id="hist-2", helpful=True)
        )
    assert exc.value.status_code == 422
    # nothing scheduled before the guard raised
    assert coros == []
    assert store.backfill_calls == []


async def test_case_feedback_422_when_nothing_referenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """referenced empty (agent cited nothing) -> nothing can be marked -> 422."""
    _patch_graph(monkeypatch, _state_values(report=_report(referenced_case_ids=[])))
    _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.case_feedback(
            "run-1", feedback.CaseFeedbackRequest(case_id="hist-1", helpful=True)
        )
    assert exc.value.status_code == 422
    assert coros == []


async def test_case_feedback_404_when_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, {"report": None, "evidence": _evidence()})
    _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.case_feedback(
            "run-1", feedback.CaseFeedbackRequest(case_id="hist-1", helpful=True)
        )
    assert exc.value.status_code == 404
    assert coros == []
