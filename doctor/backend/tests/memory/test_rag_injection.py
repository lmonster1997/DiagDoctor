"""Tests for #1 RAG injection wiring in ``_diagnosis_agent_node``.

The inner ``create_agent`` is replaced by a ``_RecordingAgent`` (captures the
``initial_messages`` it receives) so we can assert the similar-cases
HumanMessage is injected on pass 1, re-injected from cache on resume WITHOUT
re-querying, skipped when the feature flag is off, and that any retrieval
failure degrades gracefully (diagnosis still produces a report).

Only the LLM + retrieval are faked; the REAL ``_diagnosis_agent_node`` runs
(the same pattern as ``tests/graph/test_hitl.py``).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from src.config import settings
from src.engine.nodes import diagnosis_agent as diag_mod
from src.engine.state import DoctorState, NormalizedEvidence, Signal
from src.memory.long_term.case_retriever import ScoredCase

CONVERGED_JSON = """```json
{
  "primary_category": "logic",
  "categories": ["logic"],
  "symptom_tier": "backend",
  "root_cause_tier": "backend",
  "root_cause": "update_comment 未校验 owner 导致 IDOR",
  "affected_file": "app/services/comment_service.py",
  "affected_function": "update_comment",
  "fix_suggestion": "加 owner 校验",
  "evidence_chain": ["sig-1"],
  "confidence": 0.85
}
```"""


class _RecordingAgent:
    """Replaces the inner create_agent; records the messages it was invoked with."""

    def __init__(self) -> None:
        self.received_messages: list[BaseMessage] | None = None

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.received_messages = list(state.get("messages", []))
        return {"messages": [AIMessage(content=CONVERGED_JSON)]}


def _evidence() -> NormalizedEvidence:
    return NormalizedEvidence(
        user_report="创建任务后页面卡死",
        golden_signals=[
            Signal(signal_type="error_log", service_tier="backend", summary="TypeError on tags")
        ],
        trigger_time="2026-07-18T10:00:00Z",
        trigger_trace_ids=["self-trace-1"],
    )


def _state(evidence: NormalizedEvidence, **overrides: Any) -> dict[str, Any]:
    return {
        "evidence": evidence,
        "case_id": "test-case-1",
        "findings": [],
        "human_guidance": None,
        "hitl_resumed": False,
        **overrides,
    }


async def _run(state: dict[str, Any]) -> dict[str, Any]:
    """Run the real node with a dict state (cast to DoctorState for the signature)."""
    return await diag_mod._diagnosis_agent_node(cast(DoctorState, state))


def _scored(case_id: str = "hist-1") -> ScoredCase:
    return ScoredCase(
        case_id=case_id,
        score=0.82,
        relevance=0.9,
        recency=1.0,
        importance=0.4,
        payload={
            "case_id": case_id,
            "category": "frontend_crash",
            "symptom_tier": "frontend",
            "is_cross_layer": True,
            "root_cause": "TaskResponse schema missing tags field",
            "fix_suggestion": "add tags to TaskResponse",
            "confidence": 0.85,
            "source": "user_upvote",
            "user_report_snippet": "page crash on tags",
        },
    )


def _human_message_contents(agent: _RecordingAgent) -> list[str]:
    assert agent.received_messages is not None
    return [str(m.content) for m in agent.received_messages if isinstance(m, HumanMessage)]


def _patch_agent(monkeypatch: pytest.MonkeyPatch) -> _RecordingAgent:
    agent = _RecordingAgent()
    monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: agent)
    return agent


# ── pass 1: retrieve + inject + cache ───────────────────────────────


async def test_first_pass_injects_similar_cases_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _patch_agent(monkeypatch)
    calls: list[NormalizedEvidence] = []

    async def fake_search(ev: NormalizedEvidence, k_final: int = 3, *, now: Any = None) -> list[ScoredCase]:
        calls.append(ev)
        return [_scored()]

    monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)

    result = await _run(_state(_evidence()))

    # retrieval ran once
    assert len(calls) == 1
    # the similar-cases HumanMessage was injected into the agent's input
    assert any("历史相似诊断参考" in c for c in _human_message_contents(agent))
    # state updates cached the retrieved ids + formatted text
    assert result["retrieved_case_ids"] == ["hist-1"]
    assert "历史相似诊断参考" in result["similar_cases_text"]


async def test_first_pass_empty_recall_injects_nothing_but_caches_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _patch_agent(monkeypatch)

    async def fake_search(ev: NormalizedEvidence, k_final: int = 3, *, now: Any = None) -> list[ScoredCase]:
        return []

    monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)

    result = await _run(_state(_evidence()))

    assert not any("历史相似诊断参考" in c for c in _human_message_contents(agent))
    # cached as empty so resume knows not to re-inject
    assert result["retrieved_case_ids"] == []
    assert result["similar_cases_text"] == ""


# ── resume: re-inject from cache, NO re-query ───────────────────────


async def test_resume_reinjects_from_cache_without_requery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _patch_agent(monkeypatch)
    calls: list[NormalizedEvidence] = []

    async def fake_search(ev: NormalizedEvidence, k_final: int = 3, *, now: Any = None) -> list[ScoredCase]:
        calls.append(ev)
        return [_scored()]

    monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)

    cached_text = "## 历史相似诊断参考(来自知识库)\n\ncached-from-pass-1 block"
    state = _state(_evidence(), human_guidance="check the ORM layer", similar_cases_text=cached_text)

    result = await _run(state)

    # design §6.5: resume must NOT re-query
    assert calls == []
    # cached block re-injected
    assert any("cached-from-pass-1" in c for c in _human_message_contents(agent))
    # resume returns no rag_updates -> preserves pass-1 cache in persisted state
    assert "retrieved_case_ids" not in result
    assert "similar_cases_text" not in result


# ── feature flag ────────────────────────────────────────────────────


async def test_flag_off_skips_retrieval_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _patch_agent(monkeypatch)
    calls: list[NormalizedEvidence] = []

    async def fake_search(ev: NormalizedEvidence, k_final: int = 3, *, now: Any = None) -> list[ScoredCase]:
        calls.append(ev)
        return [_scored()]

    monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)
    monkeypatch.setattr(settings, "rag_injection_enabled", False)

    result = await _run(_state(_evidence()))

    assert calls == []
    assert not any("历史相似诊断参考" in c for c in _human_message_contents(agent))
    # no rag state written
    assert "retrieved_case_ids" not in result
    assert "similar_cases_text" not in result


# ── graceful degradation ────────────────────────────────────────────


async def test_retrieval_failure_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _patch_agent(monkeypatch)

    async def fake_search(ev: NormalizedEvidence, k_final: int = 3, *, now: Any = None) -> list[ScoredCase]:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)

    result = await _run(_state(_evidence()))

    # diagnosis still produced a report (RAG is a gain, not a dependency)
    assert result["report"] is not None
    assert not any("历史相似诊断参考" in c for c in _human_message_contents(agent))
    # cached as empty
    assert result["retrieved_case_ids"] == []
    assert result["similar_cases_text"] == ""
