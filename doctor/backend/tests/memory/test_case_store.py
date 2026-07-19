"""Unit tests for case_store backfill (design §8.1) - the "越用越准" write-back.

Mocks ``get_qdrant_client`` so no real Qdrant is needed. Exercises
``backfill_effectiveness``: happy path (effectiveness += delta, hit_count +1),
clamp to [0, 1] on both ends, ``hit=False`` leaves hit_count, empty input
short-circuits, retrieve failure -> 0, per-point set_payload failure -> partial
count, and skipping case_ids that retrieve no longer returns (deleted / never
indexed).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.memory.long_term import case_store


# ── Fixtures / helpers ──────────────────────────────────────────────


def _record(
    case_id: str, *, effectiveness: float = 0.0, hit_count: int = 0
) -> SimpleNamespace:
    """A minimal Qdrant Record stand-in (only the fields backfill reads)."""
    return SimpleNamespace(
        id=case_id,
        payload={"case_id": case_id, "effectiveness": effectiveness, "hit_count": hit_count},
    )


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    records: list[SimpleNamespace],
    *,
    fail_points: set[str] | None = None,
) -> SimpleNamespace:
    """Patch ``case_store.get_qdrant_client`` to serve ``records``.

    ``retrieve`` returns only the records whose id is in the requested ids
    (mimicking Qdrant omitting deleted / never-indexed ids). ``set_payload``
    records each call and raises for ids in ``fail_points`` (mimicking a
    transient per-point failure). Returns a harness holding the call log.
    """
    fail_points = fail_points or set()
    calls: list[dict[str, Any]] = []
    client = SimpleNamespace()

    async def fake_retrieve(**kwargs: Any) -> list[SimpleNamespace]:
        requested = {str(i) for i in kwargs.get("ids", [])}
        return [r for r in records if str(r.id) in requested]

    async def fake_set_payload(**kwargs: Any) -> None:
        pid = str(kwargs["points"][0])
        if pid in fail_points:
            raise RuntimeError(f"set_payload boom for {pid}")
        calls.append({"payload": kwargs["payload"], "points": kwargs["points"]})

    client.retrieve = fake_retrieve
    client.set_payload = fake_set_payload

    async def fake_get_client() -> SimpleNamespace:
        return client

    monkeypatch.setattr(case_store, "get_qdrant_client", fake_get_client)
    return SimpleNamespace(client=client, calls=calls)


# ── empty input short-circuits before touching Qdrant ──────────────


async def test_backfill_empty_case_ids_returns_zero_without_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom() -> Any:
        raise AssertionError("should not reach qdrant for empty input")

    monkeypatch.setattr(case_store, "get_qdrant_client", boom)
    assert await case_store.backfill_effectiveness([], delta=0.1, hit=True) == 0


# ── happy path: 👍 credits effectiveness + hit_count ───────────────


async def test_backfill_upvote_increments_effectiveness_and_hit_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        _record("c1", effectiveness=0.2, hit_count=1),
        _record("c2", effectiveness=0.5, hit_count=0),
    ]
    harness = _patch_client(monkeypatch, records)

    updated = await case_store.backfill_effectiveness(["c1", "c2"], delta=0.1, hit=True)

    assert updated == 2
    assert len(harness.calls) == 2
    by_id = {str(c["points"][0]): c["payload"] for c in harness.calls}
    assert by_id["c1"]["effectiveness"] == pytest.approx(0.3)
    assert by_id["c1"]["hit_count"] == 2
    assert by_id["c2"]["effectiveness"] == pytest.approx(0.6)
    assert by_id["c2"]["hit_count"] == 1


# ── clamp to [0, 1] ────────────────────────────────────────────────


async def test_backfill_clamps_effectiveness_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _patch_client(monkeypatch, [_record("c1", effectiveness=0.95, hit_count=3)])
    await case_store.backfill_effectiveness(["c1"], delta=0.1, hit=True)
    assert harness.calls[0]["payload"]["effectiveness"] == 1.0
    assert harness.calls[0]["payload"]["hit_count"] == 4


async def test_backfill_clamps_effectiveness_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _patch_client(monkeypatch, [_record("c1", effectiveness=0.05, hit_count=2)])
    await case_store.backfill_effectiveness(["c1"], delta=-0.1, hit=False)
    assert harness.calls[0]["payload"]["effectiveness"] == 0.0
    # hit=False -> hit_count unchanged
    assert harness.calls[0]["payload"]["hit_count"] == 2


# ── 👎: effectiveness down, hit_count unchanged ────────────────────


async def test_backfill_downvote_leaves_hit_count_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _patch_client(monkeypatch, [_record("c1", effectiveness=0.4, hit_count=5)])
    await case_store.backfill_effectiveness(["c1"], delta=-0.1, hit=False)
    assert harness.calls[0]["payload"]["effectiveness"] == pytest.approx(0.3)
    assert harness.calls[0]["payload"]["hit_count"] == 5


# ── graceful degradation ───────────────────────────────────────────


async def test_backfill_retrieve_failure_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace()

    async def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("qdrant 500")

    client.retrieve = boom
    client.set_payload = boom

    async def fake_get_client() -> SimpleNamespace:
        return client

    monkeypatch.setattr(case_store, "get_qdrant_client", fake_get_client)
    # retrieve raises -> 0, set_payload never reached
    assert await case_store.backfill_effectiveness(["c1"], delta=0.1) == 0


async def test_backfill_set_payload_failure_skips_point_but_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record("c1", effectiveness=0.2), _record("c2", effectiveness=0.2)]
    harness = _patch_client(monkeypatch, records, fail_points={"c1"})

    updated = await case_store.backfill_effectiveness(["c1", "c2"], delta=0.1, hit=True)

    # c1 failed -> only c2 updated; c1 failure does not abort the loop
    assert updated == 1
    assert len(harness.calls) == 1
    assert str(harness.calls[0]["points"][0]) == "c2"


async def test_backfill_skips_missing_case_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    # only c1 exists; c2 was deleted / never indexed -> retrieve omits it
    harness = _patch_client(monkeypatch, [_record("c1", effectiveness=0.2)])
    updated = await case_store.backfill_effectiveness(["c1", "c2"], delta=0.1, hit=True)
    assert updated == 1
    assert len(harness.calls) == 1
    assert str(harness.calls[0]["points"][0]) == "c1"
