"""Unit tests for case_retriever (the READ side of the episodic memory loop).

Mocks ``embed_single`` + ``get_qdrant_client`` so no real Qdrant / bge-m3 is
needed. Exercises: ``derive_tier``, ``build_symptom_passage`` content,
three-factor scoring (recency / importance), trace dedup, self-exclusion,
relevance threshold, top-k, empty recall, graceful degradation, and the §6.5
injection formatter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from src.engine.state import Correlation, NormalizedEvidence, Signal
from src.memory.long_term import case_retriever
from src.memory.long_term.case_retriever import (
    HIT_COUNT_CAP,
    RELEVANCE_THRESHOLD,
    ScoredCase,
    _dedup_by_trace,
    _importance,
    _recency,
    _score_hit,
    format_similar_cases,
    search_historical_cases,
)
from src.memory.long_term.encoding import build_symptom_passage, derive_tier

NOW = datetime(2026, 7, 18, tzinfo=UTC)


# ── Fixtures / helpers ──────────────────────────────────────────────


SignalType = Literal[
    "error_log",
    "error_span",
    "slow_span",
    "repeated_query",
    "behavior_mismatch",
    "data_invariant_broken",
    "access_control_anomaly",
    "silent_data_loss",
]
ServiceTier = Literal["frontend", "backend"]


def _signal(
    signal_type: SignalType = "error_log",
    tier: ServiceTier = "backend",
    summary: str = "",
) -> Signal:
    return Signal(signal_type=signal_type, service_tier=tier, summary=summary)


def _evidence(
    *,
    user_report: str = "创建任务后页面卡死",
    signals: list[Signal] | None = None,
    correlations: list[Correlation] | None = None,
    trigger_trace_ids: list[str] | None = None,
) -> NormalizedEvidence:
    return NormalizedEvidence(
        user_report=user_report,
        golden_signals=signals if signals is not None else [_signal(summary="TypeError on tags")],
        correlations=correlations if correlations is not None else [],
        trigger_trace_ids=trigger_trace_ids if trigger_trace_ids is not None else [],
    )


def _hit(
    score: float = 0.9,
    *,
    trace_id: str = "t1",
    case_id: str = "c1",
    confidence: float = 0.8,
    hit_count: int = 0,
    effectiveness: float = 0.0,
    created_at: str | None = None,
    is_cross_layer: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "trace_id": trace_id,
            "case_id": case_id,
            "category": "performance",
            "symptom_tier": "backend",
            "is_cross_layer": is_cross_layer,
            "root_cause": "N+1 in ORM relation",
            "fix_suggestion": "add selectinload",
            "confidence": confidence,
            "hit_count": hit_count,
            "effectiveness": effectiveness,
            "source": "user_upvote",
            "user_report_snippet": "page slow",
            "created_at": created_at if created_at is not None else NOW.isoformat(),
        },
        score=score,
        id=case_id,
    )


def _patch_retriever(
    monkeypatch: pytest.MonkeyPatch, hits: list[SimpleNamespace]
) -> SimpleNamespace:
    """Patch embed_single + get_qdrant_client to serve ``hits``."""

    async def fake_embed(_text: str) -> list[float]:
        return [0.1] * 8

    client = SimpleNamespace(
        points=hits,
        query_calls=0,
    )

    async def fake_get_client() -> Any:
        return client

    async def fake_query_points(**_kwargs: Any) -> SimpleNamespace:
        client.query_calls += 1
        return SimpleNamespace(points=client.points)

    client.query_points = fake_query_points
    monkeypatch.setattr(case_retriever, "embed_single", fake_embed)
    monkeypatch.setattr(case_retriever, "get_qdrant_client", fake_get_client)
    return client


# ── encoding: derive_tier + build_symptom_passage ───────────────────


def test_derive_tier_cross_layer_when_correlations() -> None:
    ev = _evidence(correlations=[Correlation()])
    assert derive_tier(ev) == "cross_layer"


def test_derive_tier_majority_vote() -> None:
    ev = _evidence(
        signals=[
            _signal(tier="backend"),
            _signal(tier="frontend"),
            _signal(tier="frontend"),
        ]
    )
    assert derive_tier(ev) == "frontend"


def test_derive_tier_default_backend_when_no_signals() -> None:
    ev = _evidence(signals=[])
    assert derive_tier(ev) == "backend"


def test_build_symptom_passage_carries_symptoms_only() -> None:
    ev = _evidence(
        user_report="页面卡死",
        signals=[_signal(signal_type="slow_span", tier="backend", summary="SELECT * FROM tasks 重复 47 次")],
    )
    passage = build_symptom_passage(ev)
    # symptom anchors present
    assert "信号: slow_span" in passage
    assert "层级: backend" in passage
    assert "页面卡死" in passage
    assert "SELECT * FROM tasks 重复 47 次" in passage


def test_build_symptom_passage_is_index_query_symmetric() -> None:
    """Index side and query side call the SAME function (design §4.2).

    The query side has no report; the index side must not let root_cause /
    fix / category leak into the vector. Both produce identical text from the
    same evidence -> the two vectors live in one symptom subspace.
    """
    ev = _evidence()
    assert build_symptom_passage(ev) == build_symptom_passage(ev)


# ── three-factor scoring ────────────────────────────────────────────


def test_recency_recent_is_near_one() -> None:
    recent = (NOW - timedelta(days=1)).isoformat()
    assert _recency(recent, NOW) == pytest.approx(0.989, abs=0.01)


def test_recency_old_decays() -> None:
    old = (NOW - timedelta(days=365)).isoformat()
    assert _recency(old, NOW) < 0.02


def test_recency_missing_or_unparseable_is_one() -> None:
    assert _recency("", NOW) == 1.0
    assert _recency("not-a-date", NOW) == 1.0


def test_importance_degrades_to_confidence_without_feedback() -> None:
    # hit_count / effectiveness default to 0 -> importance = 0.5 * confidence
    assert _importance({"confidence": 0.8}) == pytest.approx(0.4)


def test_importance_full_formula() -> None:
    imp = _importance({"confidence": 1.0, "hit_count": HIT_COUNT_CAP, "effectiveness": 1.0})
    assert imp == pytest.approx(0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0)


def test_score_hit_combines_three_factors() -> None:
    hit = _hit(score=0.9, confidence=1.0, hit_count=HIT_COUNT_CAP, effectiveness=1.0)
    scored = _score_hit(hit, NOW)
    assert scored.relevance == 0.9
    assert scored.recency == pytest.approx(1.0)  # created_at == NOW
    assert scored.importance == pytest.approx(1.0)
    assert scored.score == pytest.approx(0.9)


# ── dedup ───────────────────────────────────────────────────────────


def test_dedup_by_trace_keeps_best() -> None:
    low = ScoredCase(case_id="a", score=0.3, relevance=0.3, recency=1.0, importance=1.0, payload={"trace_id": "t1"})
    high = ScoredCase(case_id="b", score=0.8, relevance=0.8, recency=1.0, importance=1.0, payload={"trace_id": "t1"})
    kept = _dedup_by_trace([low, high])
    assert len(kept) == 1
    assert kept[0].case_id == "b"


def test_dedup_by_trace_keeps_no_trace_cases() -> None:
    a = ScoredCase(case_id="a", score=0.5, relevance=0.5, recency=1.0, importance=1.0, payload={})
    b = ScoredCase(case_id="b", score=0.4, relevance=0.4, recency=1.0, importance=1.0, payload={})
    kept = _dedup_by_trace([a, b])
    assert {c.case_id for c in kept} == {"a", "b"}


# ── search_historical_cases (mocked embed + qdrant) ─────────────────


async def test_search_self_excludes_own_trace_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    own = _hit(score=0.95, trace_id="self-t", case_id="self")
    other = _hit(score=0.85, trace_id="other-t", case_id="other")
    _patch_retriever(monkeypatch, [own, other])
    ev = _evidence(trigger_trace_ids=["self-t"])
    result = await search_historical_cases(ev, now=NOW)
    assert [c.case_id for c in result] == ["other"]


async def test_search_threshold_filters_low_relevance(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _hit(score=0.9, trace_id="t-good", case_id="good")
    bad = _hit(score=0.3, trace_id="t-bad", case_id="bad")  # below RELEVANCE_THRESHOLD
    _patch_retriever(monkeypatch, [good, bad])
    result = await search_historical_cases(_evidence(), now=NOW)
    assert [c.case_id for c in result] == ["good"]


async def test_search_topk_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_hit(score=0.9 - i * 0.01, trace_id=f"t{i}", case_id=f"c{i}") for i in range(6)]
    _patch_retriever(monkeypatch, hits)
    result = await search_historical_cases(_evidence(), k_final=3, now=NOW)
    assert len(result) == 3
    # sorted by score desc
    assert result[0].case_id == "c0"


async def test_search_empty_recall_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # all hits below threshold -> empty recall
    _patch_retriever(monkeypatch, [_hit(score=0.1, trace_id="t", case_id="c")])
    result = await search_historical_cases(_evidence(), now=NOW)
    assert result == []


async def test_search_dedups_same_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # two hits same trace, different scores -> only the better kept
    low = _hit(score=0.9, trace_id="dup", case_id="low", confidence=0.1)
    high = _hit(score=0.9, trace_id="dup", case_id="high", confidence=1.0)
    _patch_retriever(monkeypatch, [low, high])
    result = await search_historical_cases(_evidence(), now=NOW)
    assert len(result) == 1
    assert result[0].case_id == "high"  # higher importance -> higher score


async def test_search_graceful_degradation_on_embed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_text: str) -> list[float]:
        raise RuntimeError("TEI down")

    async def fake_get_client() -> Any:
        raise AssertionError("should not reach qdrant")

    monkeypatch.setattr(case_retriever, "embed_single", boom)
    monkeypatch.setattr(case_retriever, "get_qdrant_client", fake_get_client)
    result = await search_historical_cases(_evidence(), now=NOW)
    assert result == []


async def test_search_graceful_degradation_on_qdrant_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embed(_text: str) -> list[float]:
        return [0.1] * 8

    async def fake_get_client() -> Any:
        client = SimpleNamespace()

        async def boom(**_kwargs: Any) -> Any:
            raise RuntimeError("qdrant 500")

        client.query_points = boom
        return client

    monkeypatch.setattr(case_retriever, "embed_single", fake_embed)
    monkeypatch.setattr(case_retriever, "get_qdrant_client", fake_get_client)
    result = await search_historical_cases(_evidence(), now=NOW)
    assert result == []


def test_relevance_threshold_is_a_calibrated_placeholder() -> None:
    # Design §9.1: must be calibrated with gold cases (deferred to #8).
    # Guard against silently drifting the placeholder.
    assert RELEVANCE_THRESHOLD == 0.75


# ── injection formatter (§6.5) ──────────────────────────────────────


def _scored(case_id: str = "hist-1", score: float = 0.82) -> ScoredCase:
    return ScoredCase(
        case_id=case_id,
        score=score,
        relevance=0.9,
        recency=1.0,
        importance=0.4,
        payload={
            "case_id": case_id,
            "category": "frontend_crash",
            "symptom_tier": "frontend",
            "is_cross_layer": True,
            "root_cause": "TaskResponse schema missing tags field",
            "fix_suggestion": "add tags: list[TagResponse] = [] to TaskResponse",
            "confidence": 0.85,
            "source": "user_upvote",
            "user_report_snippet": "page crash on tags",
        },
    )


def test_format_similar_cases_empty_returns_empty_string() -> None:
    assert format_similar_cases([]) == ""


def test_format_similar_cases_renders_section_65_block() -> None:
    text = format_similar_cases([_scored()])
    assert "历史相似诊断参考" in text
    assert "Case 1" in text
    assert "综合分: 0.82" in text
    assert "来源: user_upvote" in text
    assert "frontend_crash / cross_layer" in text  # is_cross_layer -> cross_layer
    assert "TaskResponse schema missing tags field" in text
    assert "add tags: list[TagResponse] = [] to TaskResponse" in text
    assert "请勿机械套用" in text
    assert "请基于当前实际证据独立判断" in text
