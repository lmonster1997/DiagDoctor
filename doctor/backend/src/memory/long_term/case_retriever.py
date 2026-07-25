"""Historical case retrieval - the READ side of the episodic memory loop.

Closes the "write-only memory" gap (design §6 / followup-plan #1): cases
indexed by ``case_store.maybe_index_diagnosis`` (on user 👍) are retrieved
here and injected into the diagnosis agent as a few-shot reference.

P0 scope (static injection, design §6.2/§6.5):
- ``search_historical_cases(evidence, k_final=3)``: embed the symptom query
  -> Qdrant search over the ``symptom`` named vector (overfetch ``OVERFETCH``)
  -> exclude self (ALL ``trigger_trace_ids``) -> three-factor score -> dedup by
  trace_id -> relevance threshold -> MMR top-k (§7.2: balance three-factor
  relevance vs root_cause-vector redundancy, λ trade-off).
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

from qdrant_client import models

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
# Two separate relevance thresholds -- the symptom and root_cause vectors
# measure different things (symptom-text similarity vs root-cause-text
# similarity), so their good/bad separation points differ. Each is calibrated
# against its OWN label (symptom layer: same-symptom vs diff-symptom cosine
# distribution; root_cause layer: same-root vs diff-root cosine distribution).
# Calibrated from the synthetic retrieval test (docs/retrieval_test_design.md
# §4) on the DashScope qwen3.7-text-embedding model -- the same-label vs
# diff-label cosine separation point. Symptom layer ~0.60 (same-symptom min
# 0.53 / diff-symptom max 0.67); root_cause ~0.61 (same-root min 0.53 /
# diff-root max 0.69). First-pass from synthetic data; gold-case refinement
# pending (design §9.1).
SYMPTOM_RELEVANCE_THRESHOLD = 0.60  # min symptom-vector cosine to keep a hit
ROOT_CAUSE_RELEVANCE_THRESHOLD = 0.61  # min root_cause-vector cosine to keep a hit
# Back-compat alias for tests/scripts that read the old single constant name.
RELEVANCE_THRESHOLD = SYMPTOM_RELEVANCE_THRESHOLD
OVERFETCH = 10  # Qdrant limit before rerank / dedup / threshold
RECENCY_TAU_DAYS = 90.0  # exp(-Δt / τ) time-decay constant (days)
# MMR (design §7.2): symptom-layer top-k relevance-vs-diversity trade-off.
# MMR(c) = rel_norm(c) - MMR_LAMBDA * max_{selected} cosine(root_cause vectors).
# 0 = pure three-factor score (no diversity), 1 = max diversity. Uncalibrated
# (no gold diversity data) -- default 0.5 (balanced), same honest-labeling as
# RECENCY_TAU_DAYS / importance weights; tunable.
MMR_LAMBDA = 0.5


@dataclass(frozen=True)
class ScoredCase:
    """A retrieved historical case with its three-factor score."""

    case_id: str
    score: float  # relevance × recency × importance
    relevance: float
    recency: float
    importance: float
    payload: dict[str, Any]  # full Qdrant payload, for injection display


@dataclass(frozen=True)
class ConflictReport:
    """P1-c (design §7.2): does the retrieved set span multiple root causes?

    Same-symptom-different-root-cause cases are a de-anchoring risk: the agent
    might copy top-1's diagnosis/fix when history actually points several ways.
    Conflict is relational (case A vs B under symptom S), so it is detected on
    the retrieved SET at injection time, not stored per case.

    Conflict key = ``root_cause`` TEXT distinctness (normalized), NOT ``category``
    and NOT the ``root_cause`` vector: category is too coarse (BE-020/021/022 all
    ``backend_error`` -> would miss the §7.2 canonical demo) and the root_cause
    vector clusters same-area roots together (the §C ③ limitation, can't split
    same-area-different-mechanism). Only text distinctness fires on the §9.3
    "same symptom, different root" pairs AND is faithful to §7.2's "不同 root_cause".
    """

    is_conflict: bool
    n_directions: int  # count of distinct non-empty normalized root_cause texts


def _normalize_root_cause(text: str) -> str:
    """Collapse whitespace (incl. newlines) + strip, for root_cause comparison.

    root_cause texts may embed newlines (e.g. FE-020's multi-line summary); we
    compare on content, not formatting.
    """
    return " ".join((text or "").split())


def detect_conflict(cases: list[ScoredCase]) -> ConflictReport:
    """Detect whether ``cases`` span ≥2 distinct root causes (design §7.2).

    Returns a ``ConflictReport``; ``is_conflict`` is True when the retrieved set
    contains ≥2 distinct (normalized, non-empty) ``root_cause`` values -- i.e.
    history points multiple diagnostic directions and the agent must not anchor
    on top-1. Cases with empty root_cause don't count as a direction.
    """
    distinct: set[str] = set()
    for case in cases:
        norm = _normalize_root_cause(str(case.payload.get("root_cause") or ""))
        if norm:
            distinct.add(norm)
    n = len(distinct)
    return ConflictReport(is_conflict=n >= 2, n_directions=n)


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
    """0.5·confidence + 0.5·effectiveness (§6.1, two-signal).

    ``confidence`` (prior: the diagnosing LLM's confidence) +
    ``effectiveness`` (posterior: acknowledged-affinity score written by the
    case-level "有帮助" loop, §8.2). ``hit_count`` was dropped -- it was
    mathematically redundant with ``effectiveness`` (both = 👍 count) and
    carried no extra signal (§6.1). ``effectiveness`` defaults to 0 until the
    feedback loop writes it, so importance degrades to ``0.5·confidence``
    initially -- the formula is in place, the inputs arrive later.
    """
    confidence = float(payload.get("confidence", 0.0) or 0.0)
    effectiveness = float(payload.get("effectiveness", 0.0) or 0.0)
    return 0.5 * confidence + 0.5 * effectiveness


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


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity; 0.0 if either vector is missing or length-mismatched.

    Used by MMR to measure root_cause-vector redundancy between candidates.
    Returns 0.0 (not negative) for missing vectors so a candidate with no
    vector is treated as "no redundancy info" -- MMR then falls back to its
    relevance term, degenerating gracefully to score order when no vectors are
    available (e.g. tests that don't supply vectors).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _root_cause_vector_of(hit: Any) -> list[float] | None:
    """Extract the ``root_cause`` named vector from a Qdrant hit (for MMR).

    With named vectors ``query_points(with_vectors=True)`` returns
    ``point.vector`` as ``{"symptom": [...], "root_cause": [...]}``; we take the
    ``root_cause`` vector -- MMR diversifies on root-cause direction, so case-
    to-case redundancy is measured on the root_cause vector, not the symptom
    one (candidates are already symptom-similar by definition of the recall).
    """
    vec = getattr(hit, "vector", None)
    if isinstance(vec, dict):
        return vec.get(VECTOR_NAME_ROOT_CAUSE)
    if isinstance(vec, list):  # single-vector schema (not used today)
        return vec
    return None


def _select_mmr_topk(
    scored: list[ScoredCase],
    vectors: dict[str, list[float]],
    k_final: int,
    lam: float,
) -> list[ScoredCase]:
    """Symptom-layer MMR diversity selection (design §7.2, task 4).

    Maximal Marginal Relevance (Carbonell & Goldstein 1998): greedily select k
    cases that are both relevant to the query AND diverse from the already-
    selected set, balancing the two via ``lam``::

        MMR(c) = rel_norm(c) - lam * max_{s in selected} sim(c, s)

    - ``rel_norm(c)`` = three-factor score (``relevance × recency ×
      importance``) normalized by the pool max -> [0,1], so the relevance term
      is scale-comparable to the cosine redundancy term and ``lam`` in [0,1] is
      a clean trade-off. Three-factor score (not raw cosine) keeps
      recency/importance in the relevance signal.
    - ``sim(c, s)`` = cosine between ``c`` and ``s``'s ``root_cause`` named
      vectors -- the layer diversifies on ROOT-CAUSE direction (same-symptom-
      different-root is the de-anchoring case). Vectors are fetched per-query
      via ``with_vectors`` (the payload carries no vector).

    Unlike hard root_cause-text dedup, MMR SOFTLY penalizes redundancy: a
    highly-relevant same-root case can still be selected (just scored lower),
    and the pool fills to ``k_final`` with relevant cases even when few distinct
    roots exist (redundant-but-relevant, NOT noise -- §6.6's "don't pad noise"
    is about irrelevant cases, which the threshold already excluded). Hard
    text-dedup was rejected as too weak -- it only fired on literal root_cause
    text match (a near no-op on free-form LLM text and on the unique-text
    synthetic set); see design §7.2.

    ``lam`` is uncalibrated (no gold diversity data), default 0.5 (balanced),
    consistent with the project's other hand-tuned knobs (recency τ,
    importance weights) -- honestly labeled, tunable. The root-cause layer
    (``search_by_root_cause``) does NOT use this: depth wants same-root
    recalls, so it keeps pure score top-k.
    """
    if not scored:
        return []
    max_score = max(c.score for c in scored)
    if max_score <= 0.0:
        # Degenerate: all-zero three-factor scores (e.g. importance 0 across the
        # pool). No score signal to trade off -> relevance order.
        return sorted(scored, key=lambda c: c.relevance, reverse=True)[:k_final]
    rel_norm = {c.case_id: c.score / max_score for c in scored}

    selected: list[ScoredCase] = []
    remaining = list(scored)
    while remaining and len(selected) < k_final:
        best: ScoredCase | None = None
        best_mmr = -math.inf
        for c in remaining:
            if selected:
                redundancy = max(
                    _cosine(vectors.get(c.case_id), vectors.get(s.case_id)) for s in selected
                )
            else:
                redundancy = 0.0
            mmr = rel_norm[c.case_id] - lam * redundancy
            if mmr > best_mmr:
                best_mmr = mmr
                best = c
        assert best is not None  # remaining is non-empty here
        selected.append(best)
        remaining.remove(best)
    return selected


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
    relevance_threshold: float,
    query_filter: models.Filter | None = None,
    diversify: bool = False,
) -> list[ScoredCase]:
    """Shared retrieval pipeline over a named vector (symptom or root_cause).

    Pipeline (design §6.2):
    1. Qdrant ``query_points`` over ``vector_name`` (overfetch ``OVERFETCH``)
    2. exclude self (ALL ``exclude_trace_ids``, not just the first)
    3. three-factor score
    4. dedup by trace_id (keep best)
    5. relevance-threshold filter (``relevance_threshold`` -- caller picks per
       vector: symptom layer and root_cause layer have separate thresholds,
       calibrated against their own labels)
    6. top-k: ``diversify=True`` (symptom / breadth layer) runs MMR
       (``_select_mmr_topk`` -- greedily balance three-factor relevance vs
       root_cause-vector redundancy, λ trade-off); ``diversify=False``
       (root_cause / depth layer) takes pure score top-k (depth wants
       same-root recalls, MMR would penalize the very thing it seeks).

    ``query_filter`` is reserved for future structured filters; neither the
    symptom nor the root_cause branch passes one today (tier hard filter
    removed -- see ``search_historical_cases``).

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
            query_filter=query_filter,
            limit=OVERFETCH,
            with_payload=True,
            # MMR (symptom layer) needs each candidate's root_cause vector to
            # compute inter-case redundancy; the root_cause layer doesn't.
            with_vectors=diversify,
        )
        hits = result.points
    except Exception:
        logger.warning("rag_retrieval_failed", vector=vector_name, exc_info=True)
        return []

    self_ids = {str(t) for t in exclude_trace_ids if t}
    scored: list[ScoredCase] = []
    vectors: dict[str, list[float]] = {}
    for h in hits:
        if str((h.payload or {}).get("trace_id") or "") in self_ids:
            continue
        sc = _score_hit(h, now)
        scored.append(sc)
        if diversify:
            vec = _root_cause_vector_of(h)
            if vec is not None:
                vectors[sc.case_id] = vec
    scored = _dedup_by_trace(scored)
    scored = [c for c in scored if c.relevance >= relevance_threshold]
    if diversify:
        scored = _select_mmr_topk(scored, vectors, k_final, MMR_LAMBDA)
    else:
        scored = sorted(scored, key=lambda c: c.score, reverse=True)[:k_final]

    if not scored:
        logger.info(
            "rag_empty_recall",
            vector=vector_name,
            k_final=k_final,
            raw_hits=len(hits),
            self_excluded=len(self_ids),
            diversify=diversify,
        )
    else:
        logger.info(
            "rag_retrieved",
            vector=vector_name,
            k_final=k_final,
            returned=len(scored),
            top_score=scored[0].score,
            diversify=diversify,
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

    Top-k is MMR-selected (§7.2, task 4): the symptom layer is the breadth
    layer, so it runs Maximal Marginal Relevance (``_select_mmr_topk``) --
    greedily balancing three-factor relevance against root_cause-vector
    redundancy (λ trade-off) to favor multiple root-cause directions over
    same-root dupes, while still filling ``k_final`` with relevant cases when
    the pool lacks diversity (redundant-but-relevant, not noise).

    No tier payload filter: symptom recall runs before the agent has diagnosed
    anything, so the tier (``derive_tier(evidence)``) is a *guess* that can be
    wrong (correlations missed / majority-vote tie / no signals -> backend).
    A hard filter on a guessed tier would silently exclude cross-tier same-root
    cases -- the symptom branch already runs first in the chain, so a wrong tier
    means a silent 0-recall with no log. Cross-tier noise is left to the
    relevance threshold + three-factor score + P1-c conflict detection instead.
    ``derive_tier`` is retained in ``encoding`` for eval/unit tests but is no
    longer stored in the payload (tier filter reversed, §附录 B).
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
        relevance_threshold=SYMPTOM_RELEVANCE_THRESHOLD,
        # §7.2 (task 4): symptom layer is the BREADTH layer -- pick k distinct
        # root-cause directions instead of k same-root dupes. Root-cause layer
        # (search_by_root_cause) omits this (depth wants same-root recalls).
        diversify=True,
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
        relevance_threshold=ROOT_CAUSE_RELEVANCE_THRESHOLD,
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

    # P1-c (design §7.2): conflict warning when the retrieved set spans ≥2
    # distinct root causes (same symptom, different roots). De-anchors the agent
    # from top-1 -- history is ambiguous here, judge independently. Omitted when
    # all retrieved cases share one root cause (no ambiguity).
    conflict = detect_conflict(cases)
    if conflict.is_conflict:
        lines.append(
            f"⚠️ 冲突提示:历史相似症状对应 {conflict.n_directions} 种不同根因,"
            "请勿锚定单一 case 的诊断,基于当前实际证据独立核查。"
        )
        lines.append("")

    for i, case in enumerate(cases, 1):
        pl = case.payload
        report = str(pl.get("user_report_snippet") or "")[:120]
        root_cause = pl.get("root_cause") or "?"
        fix = pl.get("fix_suggestion") or pl.get("fix_snippet") or "?"
        affected_files = pl.get("affected_files") or []
        files_str = ", ".join(str(f) for f in affected_files) if affected_files else "(未记录)"
        # §8.1 path 2: expose case_id so the agent can declare which cases it
        # actually referenced (referenced_case_ids in the report JSON). Without
        # an id in the block the agent has nothing to cite -- the whole
        # reference->feedback chain depends on this.
        lines.append(f"### Case {i} [id: {case.case_id}](综合分: {case.score:.2f})")
        lines.append(f'- 用户报告: "{report}"')
        lines.append(f"- 根因: {root_cause}")
        lines.append(f"- 修复: {fix}")
        lines.append(f"- 涉及文件: {files_str}")
        lines.append("")
    lines.append("⚠️ 以上仅为历史参考,请基于当前实际证据独立判断。")
    return "\n".join(lines)
