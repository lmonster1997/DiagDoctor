"""Tests for the P1-a root-cause recall tool (``src/tools/memory_recall.py``).

The tool wraps ``case_retriever.search_by_root_cause`` + ``format_similar_cases``
with a feature-flag gate and graceful degradation. These tests exercise the
coroutine (``search_historical_root_cause``) directly: flag off, empty
hypothesis, success, empty recall, and failure -- all return a string, never
raise. ``search_by_root_cause`` itself is mocked (its pipeline is covered by
``test_case_retriever.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.config import settings
from src.memory.long_term.case_retriever import ScoredCase
from src.tools import memory_recall


def _scored(case_id: str = "hist-1") -> ScoredCase:
    return ScoredCase(
        case_id=case_id,
        score=0.82,
        relevance=0.9,
        recency=1.0,
        importance=0.4,
        payload={
            "case_id": case_id,
            "affected_files": ["app/api/tasks.py"],
            "root_cause": "N+1: list_tasks 逐条查 comments",
            "fix_suggestion": "恢复 selectinload 预加载",
            "confidence": 0.85,
            "user_report_snippet": "看板很慢",
        },
    )


# ── feature flag ────────────────────────────────────────────────────


async def test_flag_off_returns_disabled_string_without_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_hyp: str, **_kw: Any) -> list[ScoredCase]:
        raise AssertionError("should not query when disabled")

    monkeypatch.setattr(memory_recall, "search_by_root_cause", boom)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", False)

    out = await memory_recall.search_historical_root_cause("N+1 in list_tasks")
    assert "未启用" in out


# ── input validation ────────────────────────────────────────────────


async def test_empty_hypothesis_returns_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_hyp: str, **_kw: Any) -> list[ScoredCase]:
        raise AssertionError("should not query for empty hypothesis")

    monkeypatch.setattr(memory_recall, "search_by_root_cause", boom)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", True)

    for empty in ("", "   ", " \n "):
        out = await memory_recall.search_historical_root_cause(empty)
        assert "请提供根因假设" in out


# ── success ─────────────────────────────────────────────────────────


async def test_success_returns_formatted_reference_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_hyp: str, *, k_final: int = 3) -> list[ScoredCase]:
        return [_scored()]

    monkeypatch.setattr(memory_recall, "search_by_root_cause", fake_search)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", True)

    out = await memory_recall.search_historical_root_cause("N+1 in list_tasks")
    assert "历史相似诊断参考" in out
    assert "N+1: list_tasks 逐条查 comments" in out
    assert "请勿机械套用" in out


async def test_success_passes_k_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_search(hyp: str, *, k_final: int = 3) -> list[ScoredCase]:
        captured["hyp"] = hyp
        captured["k_final"] = k_final
        return [_scored()]

    monkeypatch.setattr(memory_recall, "search_by_root_cause", fake_search)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", True)

    await memory_recall.search_historical_root_cause("  N+1 in list_tasks  ", k=5)
    # hypothesis is stripped before embedding
    assert captured["hyp"] == "N+1 in list_tasks"
    assert captured["k_final"] == 5


# ── empty recall ────────────────────────────────────────────────────


async def test_empty_recall_returns_no_match_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_hyp: str, *, k_final: int = 3) -> list[ScoredCase]:
        return []

    monkeypatch.setattr(memory_recall, "search_by_root_cause", fake_search)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", True)

    out = await memory_recall.search_historical_root_cause("some exotic root cause")
    assert "未找到" in out


# ── graceful degradation ────────────────────────────────────────────


async def test_failure_returns_degradation_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_hyp: str, *, k_final: int = 3) -> list[ScoredCase]:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(memory_recall, "search_by_root_cause", boom)
    monkeypatch.setattr(settings, "rag_root_cause_tool_enabled", True)

    out = await memory_recall.search_historical_root_cause("N+1 in list_tasks")
    assert "失败" in out
