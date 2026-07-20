"""Historical case retrieval - the READ side of the episodic memory loop.

Closes the "write-only memory" gap (design §6 / followup-plan #1): cases
indexed by ``case_store.maybe_index_diagnosis`` (on user 👍) are retrieved
here and injected into the diagnosis agent as a few-shot reference.

P0 scope (static injection, design §6.2/§6.5):
- ``search_historical_cases(evidence, k_final=3)``: embed the symptom query
  -> Qdrant search over the ``symptom`` named vector (overfetch ``OVERFETCH``)
  -> exclude self (ALL ``trigger_trace_ids``) -> three-factor score -> dedup by
  trace_id -> relevance threshold -> top-k.
- Three-factor score ``relevance × recency × importance`` (design §6.1), NOT
  pure cosine. Threshold / weights need gold-case calibration (design §9.1,
  deferred to RetrievalEvaluator #8) -- constants here are placeholders with
  a calibration TODO.
- Empty recall / any failure -> return ``[]`` (inject nothing); RAG is a
  gain, not a dependency. Structured logs ``rag_empty_recall`` /
  ``rag_retrieval_failed``.

P1-a (design §6.4, breaks the #8 symptom-similarity ceiling):
- ``search_by_root_cause(hypothesis, k_final=3)``: embed the agent's
  root-cause hypothesis -> Qdrant search over the ``root_cause`` named vector
  (same three-factor / dedup / threshold pipeline). The agent forms a root-cause
  hypothesis mid-investigation, then queries root-cause similarity -- getting
  "same root cause" recalls that symptom-similarity misses (same-root-diff-
  symptom) and avoiding "same symptom, different root" over-recall. Exposed as
  an agent tool (``src/tools/memory_recall.py``); P0 symptom static injection
  stays on its own ``rag_injection_enabled`` switch.

P1 (NOT here): semantic pattern (§3.2), failed-case negative (§8.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.engine.state import NormalizedEvidence
from src.memory.long_term.embedding import embed_single
from src.memory.long_term.encoding import build_symptom_passage
from src.memory.long_term.qdrant_client import (
    COLLECTION_NAME,
    VECTOR_NAME_ROOT_CAUSE,
    VECTOR_NAME_SYMPTOM,
    get_qdrant_client,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Retrieval constants ─────────────────────────────────────────────
# All three need gold-case calibration (design §9.1, deferred to #8).
RELEVANCE_THRESHOLD = 0.75  # min cosine relevance to keep a hit
OVERFETCH = 10  # Qdrant limit before rerank / dedup / threshold
RECENCY_TAU_DAYS = 90.0  # exp(-Δt / τ) time-decay constant (days)
HIT_COUNT_CAP = 10  # saturate hit_count normalization (importance factor)


@dataclass(frozen=True)
class ScoredCase:
    """A retrieved historical case with its three-factor score."""

    case_id: str
    score: float  # relevance × recency × importance
    relevance: float
    recency: float
    importance: float
    payload: dict[str, Any]  # full Qdrant payload, for injection display


# ═════════════════════════════════════════════════════════════════════
# Three-factor scoring (design §6.1)
# ═════════════════════════════════════════════════════════════════════


def _recency(created_at: str, now: datetime) -> float:
    """exp(-Δt / τ) where Δt is days since ``created_at``. Newer -> closer to 1.

    Unparseable / missing timestamp -> 1.0 (don't penalize; let relevance
    decide rather than dropping a case over a bad timestamp).
    """
    if not created_at:
        return 1.0
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 1.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    delta_days = max((now - created).total_seconds() / 86400.0, 0.0)
    return math.exp(-delta_days / RECENCY_TAU_DAYS)


def _importance(payload: dict[str, Any]) -> float:
    """0.5·confidence + 0.3·normalize(hit_count) + 0.2·effectiveness (§6.1).

    ``hit_count`` / ``effectiveness`` default to 0 until the feedback loop
    (#8 / §8.1) writes them, so importance degrades to ``0.5·confidence``
    initially -- the formula is in place, the inputs arrive later.
    """
    confidence = float(payload.get("confidence", 0.0) or 0.0)
    hit_count = float(payload.get("hit_count", 0) or 0)
    effectiveness = float(payload.get("effectiveness", 0.0) or 0.0)
    norm_hits = min(hit_count / HIT_COUNT_CAP, 1.0) if HIT_COUNT_CAP > 0 else 0.0
    return 0.5 * confidence + 0.3 * norm_hits + 0.2 * effectiveness


def _score_hit(hit: Any, now: datetime) -> ScoredCase:
    """Build a ScoredCase from a Qdrant hit (carrying its payload + scores)."""
    payload: dict[str, Any] = dict(hit.payload or {})
    relevance = float(hit.score) if hit.score is not None else 0.0
    recency = _recency(str(payload.get("created_at", "")), now)
    importance = _importance(payload)
    score = relevance * recency * importance
    case_id = str(payload.get("case_id") or payload.get("run_id") or hit.id)
    return ScoredCase(
        case_id=case_id,
        score=score,
        relevance=relevance,
        recency=recency,
        importance=importance,
        payload=payload,
    )


def _dedup_by_trace(scored: list[ScoredCase]) -> list[ScoredCase]:
    """Keep only the best-scoring case per ``trace_id`` (design §6.2 step 2).

    Same-trace cases (one bug diagnosed multiple times) would otherwise crowd
    out top-k. Cases with no ``trace_id`` are all kept (no dedup key).
    """
    best: dict[str, ScoredCase] = {}
    keep: list[ScoredCase] = []
    for case in scored:
        tid = str(case.payload.get("trace_id") or "")
        if not tid:
            keep.append(case)
            continue
        prev = best.get(tid)
        if prev is None or case.score > prev.score:
            best[tid] = case
    return keep + list(best.values())


# ═════════════════════════════════════════════════════════════════════
# Shared search pipeline (design §6.2) - symptom & root_cause share this
# ═════════════════════════════════════════════════════════════════════


async def _search_named_vector(
    *,
    query_vec: list[float],
    vector_name: str,
    exclude_trace_ids: list[str] | tuple[str, ...] | set[str],
    k_final: int,
    now: datetime,
) -> list[ScoredCase]:
    """Shared retrieval pipeline over a named vector (symptom or root_cause).

    Pipeline (design §6.2):
    1. Qdrant ``query_points`` over ``vector_name`` (overfetch ``OVERFETCH``)
    2. exclude self (ALL ``exclude_trace_ids``, not just the first)
    3. three-factor score
    4. dedup by trace_id (keep best)
    5. relevance-threshold filter
    6. top-k by three-factor score

    Any Qdrant error -> ``[]`` + ``rag_retrieval_failed`` log (RAG is a gain,
    not a dependency). Embedding errors are handled by the caller (it has the
    context to log which vector / query failed).
    """
    try:
        client = await get_qdrant_client()
        result = await client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vec,
            using=vector_name,  # P1-a: pick the named vector
            limit=OVERFETCH,
            with_payload=True,
        )
        hits = result.points
    except Exception:
        logger.warning("rag_retrieval_failed", vector=vector_name, exc_info=True)
        return []

    self_ids = {str(t) for t in exclude_trace_ids if t}
    scored = [
        _score_hit(h, now)
        for h in hits
        if str((h.payload or {}).get("trace_id") or "") not in self_ids
    ]
    scored = _dedup_by_trace(scored)
    scored = [c for c in scored if c.relevance >= RELEVANCE_THRESHOLD]
    scored = sorted(scored, key=lambda c: c.score, reverse=True)[:k_final]

    if not scored:
        logger.info(
            "rag_empty_recall",
            vector=vector_name,
            k_final=k_final,
            raw_hits=len(hits),
            self_excluded=len(self_ids),
        )
    else:
        logger.info(
            "rag_retrieved",
            vector=vector_name,
            k_final=k_final,
            returned=len(scored),
            top_score=scored[0].score,
        )

    return scored


# ═════════════════════════════════════════════════════════════════════
# Public entry points
# ═════════════════════════════════════════════════════════════════════


async def search_historical_cases(
    evidence: NormalizedEvidence,
    k_final: int = 3,
    *,
    now: datetime | None = None,
) -> list[ScoredCase]:
    """Retrieve the top-k symptom-similar historical cases for ``evidence`` (P0).

    Embeds the symptom query (``build_symptom_passage``) and searches the
    ``symptom`` named vector, excluding self (ALL ``evidence.trigger_trace_ids``).
    Used for the §6.5 static injection in ``_diagnosis_agent_node``.
    """
    now = now or datetime.now(UTC)

    try:
        query_vec = await embed_single(build_symptom_passage(evidence))
    except Exception:
        logger.warning("rag_retrieval_failed", vector=VECTOR_NAME_SYMPTOM, exc_info=True)
        return []

    return await _search_named_vector(
        query_vec=query_vec,
        vector_name=VECTOR_NAME_SYMPTOM,
        exclude_trace_ids=evidence.trigger_trace_ids,
        k_final=k_final,
        now=now,
    )


async def search_by_root_cause(
    hypothesis: str,
    k_final: int = 3,
    *,
    now: datetime | None = None,
    exclude_trace_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[ScoredCase]:
    """Retrieve the top-k root-cause-similar historical cases (P1-a, §6.4).

    Embeds the agent's root-cause ``hypothesis`` and searches the ``root_cause``
    named vector -- getting "same root cause" recalls that symptom-similarity
    misses. This is the tool-ified half: the agent calls it (via
    ``search_historical_root_cause`` tool) after forming a root-cause hypothesis,
    breaking the #8 symptom-similarity ceiling.

    Self-exclusion is OPTIONAL here (defaults to none): the current case isn't
    indexed yet (👍 hasn't happened), so it can't be recalled; and a prior
    diagnosis of the same bug (same ``trace_id``) being recalled is the *ideal*
    "越用越准" case (the system remembers solving this exact root cause). Callers
    that want to exclude specific traces (e.g. tests) may pass ``exclude_trace_ids``.

    Empty recall / any failure -> ``[]`` (RAG is a gain, not a dependency).
    """
    now = now or datetime.now(UTC)

    try:
        query_vec = await embed_single(hypothesis)
    except Exception:
        logger.warning("rag_retrieval_failed", vector=VECTOR_NAME_ROOT_CAUSE, exc_info=True)
        return []

    return await _search_named_vector(
        query_vec=query_vec,
        vector_name=VECTOR_NAME_ROOT_CAUSE,
        exclude_trace_ids=exclude_trace_ids or [],
        k_final=k_final,
        now=now,
    )


# ═════════════════════════════════════════════════════════════════════
# Injection formatting (design §6.5)
# ═════════════════════════════════════════════════════════════════════


def format_similar_cases(cases: list[ScoredCase]) -> str:
    """Render scored cases as the §6.5 few-shot reference markdown block.

    Returns ``""`` for empty input (caller skips injection). The block is a
    *reference of diagnostic approach*, not a verdict to copy -- the closing
    warning reminds the agent to judge independently.
    """
    if not cases:
        return ""

    lines = [
        "## 历史相似诊断参考(来自知识库)",
        "",
        "以下是与当前问题相似的已解决 Bug,仅供参考其诊断思路,请勿机械套用:",
        "",
    ]
    for i, case in enumerate(cases, 1):
        pl = case.payload
        category = pl.get("category") or "?"
        tier = "cross_layer" if pl.get("is_cross_layer") else (pl.get("symptom_tier") or "?")
        report = str(pl.get("user_report_snippet") or "")[:120]
        root_cause = pl.get("root_cause") or "?"
        fix = pl.get("fix_suggestion") or pl.get("fix_snippet") or "?"
        source = pl.get("source") or "?"
        lines.append(f"### Case {i}(综合分: {case.score:.2f},来源: {source})")
        lines.append(f'- 用户报告: "{report}"')
        lines.append(f"- 类别: {category} / {tier}")
        lines.append(f"- 根因: {root_cause}")
        lines.append(f"- 修复: {fix}")
        lines.append("")
    lines.append("⚠️ 以上仅为历史参考,请基于当前实际证据独立判断。")
    return "\n".join(lines)
