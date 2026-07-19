"""Tests for the feedback API §8.1 wiring (the "越用越准" loop).

Mocks the LangGraph checkpoint (``get_copilotkit_graph``.aget_state) and the
case_store write-back (``maybe_index_diagnosis`` / ``backfill_effectiveness``).
The fire-and-forget ``asyncio.create_task`` is captured so the test can await
the background coroutine deterministically.

Covers: upvote indexes the new case AND backfills recalled cases; backfill
runs even when indexing is hard-guard-skipped (the §8.1 invariant -- a 👍
endorses the diagnosis, which validates the references regardless of whether
the new case landed); upvote with no recalled cases skips backfill; 404 when
no report; downvote backfills with delta=-0.1 / hit=False and never indexes.
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


def _report() -> DiagnosisReport:
    return DiagnosisReport(
        root_cause="update_comment 未校验 owner",
        affected_file="app/services/comment_service.py",
        fix_suggestion="加 owner 校验",
        confidence=0.85,
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


def _patch_store(
    monkeypatch: pytest.MonkeyPatch, *, index_return: bool = True
) -> SimpleNamespace:
    """Patch maybe_index_diagnosis + backfill_effectiveness; record calls."""
    index_calls: list[dict[str, Any]] = []
    backfill_calls: list[dict[str, Any]] = []

    async def fake_index(**kwargs: Any) -> bool:
        index_calls.append(kwargs)
        return index_return

    async def fake_backfill(
        case_ids: list[str], *, delta: float, hit: bool
    ) -> int:
        backfill_calls.append({"case_ids": list(case_ids), "delta": delta, "hit": hit})
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


async def test_upvote_indexes_new_case_and_backfills_recalled_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert store.index_calls[0]["source"] == "user_upvote"
    assert store.index_calls[0]["trace_id"] == "t-1"
    # recalled cases credited (§8.1)
    assert len(store.backfill_calls) == 1
    assert store.backfill_calls[0]["case_ids"] == ["hist-1", "hist-2"]
    assert store.backfill_calls[0]["delta"] == pytest.approx(0.1)
    assert store.backfill_calls[0]["hit"] is True


async def test_upvote_with_no_recalled_cases_skips_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=[]))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    await feedback.upvote("run-1")
    await coros[0]

    assert len(store.index_calls) == 1  # still indexes the new case
    assert store.backfill_calls == []


async def test_upvote_backfills_even_when_indexing_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.1 invariant: backfill is independent of new-case indexing.

    A 👍 endorses the diagnosis -> the recalled references are validated
    whether or not the new case itself landed (e.g. hard-guard skip).
    """
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=["hist-1"]))
    store = _patch_store(monkeypatch, index_return=False)  # hard guard skipped
    coros = _capture_tasks(monkeypatch)

    await feedback.upvote("run-1")
    await coros[0]

    assert len(store.index_calls) == 1
    assert store.index_calls[0]["case_id"] == "run-1"
    # backfill still ran
    assert len(store.backfill_calls) == 1
    assert store.backfill_calls[0]["case_ids"] == ["hist-1"]


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


async def test_downvote_backfills_with_negative_delta_and_hit_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=["hist-1"]))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    resp = await feedback.downvote("run-1")
    assert resp == {"ok": True, "run_id": "run-1"}
    assert len(coros) == 1
    await coros[0]

    # 👎 never indexes a new case
    assert store.index_calls == []
    # but downgrades effectiveness on the recalled cases
    assert len(store.backfill_calls) == 1
    assert store.backfill_calls[0]["case_ids"] == ["hist-1"]
    assert store.backfill_calls[0]["delta"] == pytest.approx(-0.1)
    assert store.backfill_calls[0]["hit"] is False


async def test_downvote_with_no_recalled_cases_skips_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_graph(monkeypatch, _state_values(retrieved_case_ids=[]))
    store = _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    await feedback.downvote("run-1")
    # downvote only schedules a task when there are recalled cases
    assert coros == []
    assert store.backfill_calls == []


async def test_downvote_404_when_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, {"report": None, "evidence": _evidence()})
    _patch_store(monkeypatch)
    coros = _capture_tasks(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await feedback.downvote("run-1")
    assert exc.value.status_code == 404
    assert coros == []
