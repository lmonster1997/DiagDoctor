"""End-to-end retrieval verification over the synthetic test library.

What this script does (design: docs/retrieval_test_design.md):
  1. Load the 96 synthetic cases from tests/fixtures/retrieval_cases/cases.yaml.
  2. Build a SEPARATE test collection (``historical_cases_test``) -- never
     touches the dev ``historical_cases`` library. Rebuild-and-reseed each run.
  3. Leave-one-out: for each case Q, index the other 95 (real embed + Qdrant
     upsert via ``maybe_index_diagnosis``), then query BOTH layers using Q's
     text -- symptom layer (``search_historical_cases``, user_report) and
     root_cause layer (``search_by_root_cause``, root_cause_summary).
  4. Evaluate the four propositions (P1-P4) by case labels, per layer.
  5. Emit four cosine distributions (same/diff-symptom × same/diff-root) to
     calibrate the two per-layer thresholds (SYMPTOM_/ROOT_CAUSE_RELEVANCE_).

Calls DOCTOR'S OWN retrieval code (``search_historical_cases`` /
``search_by_root_cause`` / ``maybe_index_diagnosis``) -- this script writes NO
retrieval logic of its own. It only builds data, feeds queries, collects
results, and scores by label. So it verifies the real pipeline (overfetch /
trace dedup / three-factor / threshold / top-k), not a re-implementation.

Isolation: monkeypatches ``COLLECTION_NAME`` in case_store + case_retriever to
``historical_cases_test`` for the whole run. Dev library is untouched.

Determinism choices (per user decisions):
- No created_at time spread -> recency factor is constant 1.0 (recency is
  deterministic, not under test here).
- All cases get the same confidence -> importance = 0.5*conf is constant, so
  the three-factor score degenerates to pure relevance ranking. This script
  verifies threshold + dual-vector + relevance pipeline, NOT recency/importance
  ranking (those need time/feedback-loop data the cold-start library lacks).

Environment: DashScope embedding API (configured in .env: EMBEDDING_BASE_URL +
DASHSCOPE_API_KEY + EMBEDDING_MODEL=qwen3.7-text-embedding, dim 1024). Qdrant
must be up (http://127.0.0.1:6333). No TEI/local bge-m3 needed.

Usage (from doctor/backend)::

    PYTHONIOENCODING=utf-8 uv run python scripts/verify_retrieval_dual_vector.py
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Embedder: .env configures the DashScope API (EMBEDDING_BASE_URL +
# DASHSCOPE_API_KEY) -> embedding.py routes to the API automatically. No
# bge-m3 env vars needed (legacy path only when EMBEDDING_BASE_URL is empty).
import yaml  # noqa: E402

from src.engine.state import DiagnosisReport, NormalizedEvidence, Signal  # noqa: E402
from src.memory.long_term import case_retriever as cr  # noqa: E402
from src.memory.long_term import case_store as cs  # noqa: E402
from src.memory.long_term.case_retriever import (  # noqa: E402
    MMR_LAMBDA,
    ROOT_CAUSE_RELEVANCE_THRESHOLD,
    SYMPTOM_RELEVANCE_THRESHOLD,
    search_by_root_cause,
    search_historical_cases,
)
from src.memory.long_term.case_store import maybe_index_diagnosis  # noqa: E402
from src.memory.long_term.qdrant_client import (  # noqa: E402
    _collection_exists,
    _create_collection_internal,
    get_qdrant_client,
)

# ── Embed cache (script-scoped) ──────────────────────────────────────
# DashScope (like bge-m3) is deterministic: same text -> same vector. Leave-
# one-out re-indexes the same 96 texts 95x (9120 embeds) -> with this cache,
# ~192 unique API calls. Cached result is byte-identical to a fresh embed, so
# no pipeline behavior changes -- only redundant API calls (and cost) drop.
_embed_cache: dict[str, list[float]] = {}
_orig_embed_texts = cs.embed_texts  # capture before patching


async def _cached_embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    missing = [t for t in texts if t not in _embed_cache]
    if missing:
        fresh = await _orig_embed_texts(missing)
        for text, vec in zip(missing, fresh, strict=True):
            _embed_cache[text] = vec
    return [_embed_cache[t] for t in texts]


async def _cached_embed_single(text: str) -> list[float]:
    return (await _cached_embed_texts([text]))[0]


SEP = "=" * 72
TEST_COLLECTION = "historical_cases_test"
FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "retrieval_cases" / "cases.yaml"
)

# Determinism: uniform confidence -> importance = 0.5*conf constant -> three-factor
# degenerates to pure relevance ranking (recency/importance not under test).
UNIFORM_CONFIDENCE = 0.8
K_FINAL = 3
# Leave-one-out collects ALL candidates (threshold=0 + OVERFETCH bumped) so the
# cosine distribution is full and the threshold can be calibrated from data.
# recall@K_FINAL is the top-K_FINAL slice of the full ranked list (raw ranking
# quality, independent of the placeholder threshold -- which would otherwise
# starve calibration by filtering everything before collection).
K_CALIBRATE = 100


# ── Data loading ─────────────────────────────────────────────────────


@dataclass
class TestCase:
    case_id: str
    user_report: str
    root_cause_summary: str
    root_cause_type: str
    symptom_type: str
    tier: str
    cross_tier: bool


def load_cases() -> list[TestCase]:
    data = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [TestCase(**{k: c[k] for k in TestCase.__annotations__}) for c in data["cases"]]


# ── Indexing (real maybe_index_diagnosis, test collection) ───────────


def _report(root_cause: str) -> DiagnosisReport:
    """Minimal DiagnosisReport passing the completeness hard guard.

    root_cause + affected_file + fix_suggestion all non-empty (case_store hard
    guard). confidence uniform so importance is constant (see module docstring).
    """
    return DiagnosisReport(
        primary_category="test",
        root_cause=root_cause,
        affected_file="app/test.py",
        fix_suggestion="test fix",
        confidence=UNIFORM_CONFIDENCE,
    )


def _evidence(case: TestCase) -> NormalizedEvidence:
    """Evidence whose user_report = case symptom text (index=query symmetric).

    A single golden_signal carries the case tier so derive_tier (index-side
    payload label) matches the case's tier; signal_types left generic (not
    filtered post tier-filter removal).
    """
    return NormalizedEvidence(
        user_report=case.user_report,
        golden_signals=[Signal(signal_type="error_log", service_tier=case.tier, summary="")],
        trigger_trace_ids=[case.case_id],  # self-exclusion key = case_id
    )


async def index_cases(cases: list[TestCase]) -> int:
    """Index all given cases into the test collection. Returns count indexed."""
    n = 0
    for case in cases:
        ok = await maybe_index_diagnosis(
            report=_report(case.root_cause_summary),
            evidence=_evidence(case),
            trace_id=case.case_id,
            case_id=str(uuid.uuid4()),
        )
        n += 1 if ok else 0
    return n


# ── Collection lifecycle (test-only, isolated) ───────────────────────


async def reset_test_collection() -> None:
    """Drop + recreate the TEST collection (never touches dev library)."""
    client = await get_qdrant_client()
    if await _collection_exists(TEST_COLLECTION):
        await client.delete_collection(TEST_COLLECTION)
        print(f"  dropped existing {TEST_COLLECTION}")
    await _create_collection_internal(TEST_COLLECTION)
    print(f"  created {TEST_COLLECTION} (named vectors: symptom + root_cause)")


# ── Evaluation ───────────────────────────────────────────────────────


@dataclass
class LayerResult:
    """One layer's recall outcome for one query case."""

    query_id: str
    layer: str  # "symptom" | "root_cause"
    # ALL candidates returned (threshold=0, large overfetch), ranked by score.
    # Calibration reads relevances; recall@K_FINAL takes the top-K_FINAL slice.
    recalled_ids: list[str]
    recalled_relevances: list[float]  # cosine per candidate, parallel to ids
    recalled_scores: list[float]  # three-factor scores, parallel to ids


@dataclass
class CosinePair:
    """A (query, candidate) cosine + its label relation, for distribution."""

    layer: str
    cosine: float
    same_root: bool
    same_symptom: bool


def _quadrant(same_root: bool, same_symptom: bool) -> str:
    if same_root and same_symptom:
        return "P1"
    if same_root:
        return "P2"
    if same_symptom:
        return "P3"
    return "P4"


@dataclass
class EvalReport:
    # per-layer per-quadrant: list of (recall_hit_bool, total_candidates_in_quad)
    layer_quad: dict[str, dict[str, list[tuple[bool, int]]]] = field(default_factory=dict)
    cosine_pairs: list[CosinePair] = field(default_factory=list)


# ── Leave-one-out main ───────────────────────────────────────────────


async def leave_one_out(cases: list[TestCase]) -> tuple[dict[str, list[LayerResult]], EvalReport]:
    """Run leave-one-out: each case queries against a library of all 96.

    Returns (per-layer list of LayerResult, EvalReport with cosine pairs).

    Indexes all 96 ONCE; each query excludes itself by trace_id (``search_*``
    self-excludes via ``exclude_trace_ids`` / ``evidence.trigger_trace_ids``).
    Equivalent to re-indexing the 95 non-query cases each round -- the query
    sees the same 95-candidate library either way -- without 96x drop/recreate/
    95-upsert that stressed Qdrant's connection pool (transient ConnectError
    past round 80). Cost: 96 index ops + 96*2 searches; embed cached (~192
    unique API calls). Runs in well under a minute.
    """
    by_id = {c.case_id: c for c in cases}
    results: dict[str, list[LayerResult]] = {"symptom": [], "root_cause": []}
    report = EvalReport()

    # Index all 96 once; queries self-exclude by trace_id (equivalent to
    # leave-one-out, without per-round re-indexing).
    await reset_test_collection()
    indexed = await index_cases(cases)
    print(f"  indexed {indexed}/{len(cases)} (once; queries self-exclude by trace_id)")

    total = len(cases)
    for i, query in enumerate(cases, 1):
        if i == 1 or i % 10 == 0 or i == total:
            print(f"  [query {i}/{total}] {query.case_id}")

        # ── symptom layer: query with user_report (collect ALL for calibration) ──
        sym_scored = await search_historical_cases(_evidence(query), k_final=K_CALIBRATE)
        results["symptom"].append(
            LayerResult(
                query.case_id,
                "symptom",
                [s.payload.get("trace_id") for s in sym_scored],
                [s.relevance for s in sym_scored],
                [s.score for s in sym_scored],
            )
        )

        # ── root_cause layer: query with root_cause_summary (gold hypothesis) ──
        rc_scored = await search_by_root_cause(
            query.root_cause_summary, k_final=K_CALIBRATE, exclude_trace_ids=[query.case_id]
        )
        results["root_cause"].append(
            LayerResult(
                query.case_id,
                "root_cause",
                [s.payload.get("trace_id") for s in rc_scored],
                [s.relevance for s in rc_scored],
                [s.score for s in rc_scored],
            )
        )

        # ── collect cosine pairs for threshold calibration ──
        # relevance == cosine here (importance constant, recency constant ->
        # score = relevance * const, so relevance ~= score/const; but the raw
        # cosine is the hit's .score before three-factor. We stored relevance on
        # ScoredCase -- use it directly as the cosine signal.)
        for s in sym_scored:
            # match via trace_id (fixture case_id) -- the point's case_id is a
            # random UUID (Qdrant point id), but trace_id carries the fixture id
            cand = by_id.get(s.payload.get("trace_id"))
            if cand:
                report.cosine_pairs.append(
                    CosinePair(
                        "symptom",
                        s.relevance,
                        same_root=cand.root_cause_type == query.root_cause_type,
                        same_symptom=cand.symptom_type == query.symptom_type,
                    )
                )
        for s in rc_scored:
            cand = by_id.get(s.payload.get("trace_id"))
            if cand:
                report.cosine_pairs.append(
                    CosinePair(
                        "root_cause",
                        s.relevance,
                        same_root=cand.root_cause_type == query.root_cause_type,
                        same_symptom=cand.symptom_type == query.symptom_type,
                    )
                )

    return results, report


# ── Scoring + reporting ──────────────────────────────────────────────


def score_propositions(
    results: dict[str, list[LayerResult]], cases: list[TestCase]
) -> dict[str, dict[str, dict[str, float]]]:
    """Per layer, per quadrant: recall@k.

    For each query, partition its library candidates by quadrant (label
    relation to query), then recall@k = |quad candidates in top-k| / |quad
    candidates total|, averaged over queries that have >=1 candidate in that
    quadrant.
    """
    by_id = {c.case_id: c for c in cases}
    layer_quad: dict[str, dict[str, list[float]]] = {
        lyr: {"P1": [], "P2": [], "P3": [], "P4": []} for lyr in results
    }

    for layer, layer_results in results.items():
        for lr in layer_results:
            query = by_id[lr.query_id]
            # recall@K_FINAL: top-K_FINAL slice of the full ranked list
            # (collected at threshold=0; the operating-point threshold is
            # calibrated separately from the cosine distribution below).
            recalled = set(lr.recalled_ids[:K_FINAL])
            # partition library (all cases except query) by quadrant
            quad_total: dict[str, int] = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
            quad_hit: dict[str, int] = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
            for cand in cases:
                if cand.case_id == query.case_id:
                    continue
                q = _quadrant(
                    cand.root_cause_type == query.root_cause_type,
                    cand.symptom_type == query.symptom_type,
                )
                quad_total[q] += 1
                if cand.case_id in recalled:
                    quad_hit[q] += 1
            for q in quad_total:
                if quad_total[q] > 0:
                    layer_quad[layer][q].append(quad_hit[q] / quad_total[q])

    return {
        layer: {q: (statistics.mean(vals) if vals else 0.0) for q, vals in quads.items()}
        for layer, quads in layer_quad.items()
    }


def _dist_stats(values: list[float]) -> tuple[float, float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(values), statistics.mean(values), statistics.median(values), max(values))


def _suggest_threshold(same: list[float], diff: list[float]) -> float:
    """Crude separation point: midpoint between same-min and diff-max."""
    if not same or not diff:
        return 0.75
    lo = min(same)
    hi = max(diff)
    if lo > hi:  # cleanly separable
        return (lo + hi) / 2
    # overlap: pick the point maximizing separation (mid of overlap region)
    return (lo + hi) / 2


def report_thresholds(report: EvalReport) -> None:
    """Print the four cosine distributions + suggested per-layer thresholds."""
    print("\n" + SEP)
    print("阈值标定(四 cosine 分布 + 候选分离点)")
    print(SEP)

    for layer in ("symptom", "root_cause"):
        pairs = [p for p in report.cosine_pairs if p.layer == layer]
        # symptom layer: separate by symptom label; root_cause layer: by root label
        if layer == "symptom":
            same = [p.cosine for p in pairs if p.same_symptom]
            diff = [p.cosine for p in pairs if not p.same_symptom]
            label = "症状"
            cur = SYMPTOM_RELEVANCE_THRESHOLD
        else:
            same = [p.cosine for p in pairs if p.same_root]
            diff = [p.cosine for p in pairs if not p.same_root]
            label = "根因"
            cur = ROOT_CAUSE_RELEVANCE_THRESHOLD

        s_min, s_mean, s_med, s_max = _dist_stats(same)
        d_min, d_mean, d_med, d_max = _dist_stats(diff)
        suggested = _suggest_threshold(same, diff)
        sep = "可分离" if (same and diff and min(same) > max(diff)) else "有重叠"

        print(f"\n  [{layer} 层 / {label}标签] 召回候选 cosine 分布 (n={len(pairs)})")
        print(
            f"    同{label}对: n={len(same)}  min={s_min:.3f} mean={s_mean:.3f} "
            f"median={s_med:.3f} max={s_max:.3f}"
        )
        print(
            f"    异{label}对: n={len(diff)}  min={d_min:.3f} mean={d_mean:.3f} "
            f"median={d_med:.3f} max={d_max:.3f}"
        )
        print(f"    当前阈值={cur:.2f}  候选分离点={suggested:.3f}  ({sep})")


def report_propositions(scores: dict[str, dict[str, dict[str, float]]]) -> None:
    """Print the P1-P4 recall@k table per layer."""
    print("\n" + SEP)
    print(f"四命题评估 (recall@{K_FINAL},两层解耦各管各的)")
    print(SEP)
    print("\n  症状层(查症状相似,只看症状标签):")
    print("    命题 | 含义                     | recall@k | 期望")
    print("    -----|--------------------------|----------|------")
    exp_sym = {
        "P1": "高(同症状该召回)",
        "P2": "低(异症状不召)",
        "P3": "高(同症状多根因思路)",
        "P4": "低(异症状不召)",
    }
    for q in ("P1", "P2", "P3", "P4"):
        v = scores["symptom"][q]
        print(f"    {q}   | {_quad_desc(q, 'symptom'):<24} | {v:>8.2f} | {exp_sym[q]}")

    print("\n  根因层(查根因相似,只看根因标签):")
    print("    命题 | 含义                     | recall@k | 期望")
    print("    -----|--------------------------|----------|------")
    exp_rc = {
        "P1": "高(同根因该召回)",
        "P2": "高(同根因跨症状复用)",
        "P3": "低(异根因不召)",
        "P4": "低(异根因不召)",
    }
    for q in ("P1", "P2", "P3", "P4"):
        v = scores["root_cause"][q]
        print(f"    {q}   | {_quad_desc(q, 'root_cause'):<24} | {v:>8.2f} | {exp_rc[q]}")


def _quad_desc(q: str, layer: str) -> str:
    return {
        "P1": "同根因+同症状",
        "P2": "同根因+异症状",
        "P3": "异根因+同症状",
        "P4": "异根因+异症状",
    }[q]


# ── Layer-level metrics (precision / MRR / hit / aggregated recall) ───
# "Relevant" is defined per layer's job: symptom layer -> same symptom_type
# (the layer's job is broad same-symptom recall); root_cause layer -> same
# root_cause_type (deep same-root recall). P1-P4 per-quadrant table above shows
# the breakdown; these are the layer-aggregate quality signals.


def _layer_same_label(layer: str, cand: TestCase, query: TestCase) -> bool:
    """Same-label for this layer: symptom layer -> same symptom_type;
    root_cause layer -> same root_cause_type."""
    if layer == "symptom":
        return cand.symptom_type == query.symptom_type
    return cand.root_cause_type == query.root_cause_type


def score_layer_metrics(
    results: dict[str, list[LayerResult]], cases: list[TestCase]
) -> dict[str, dict[str, float]]:
    """Per layer: Precision@k, HitRate@k, MRR, same-label Recall@k, noise@k.

    - Precision@k: fraction of top-k that are same-label (higher = sharper).
    - HitRate@k: fraction of queries with >=1 same-label in top-k.
    - MRR: 1/rank of first same-label in the FULL ranked list (ranking quality).
    - same-label Recall@k: (same-label in top-k) / (total same-label) -- k-bounded
      (max = k/total_same); raise k or add rerank to recall more same-label.
    - noise@k: fraction of top-k that are diff-label (= 1 - Precision@k).
    """
    by_id = {c.case_id: c for c in cases}
    out: dict[str, dict[str, float]] = {}
    for layer, layer_results in results.items():
        precisions: list[float] = []
        hit_rates: list[float] = []
        rrs: list[float] = []
        same_recalls: list[float] = []
        for lr in layer_results:
            query = by_id[lr.query_id]
            library = [c for c in cases if c.case_id != query.case_id]
            total_same = sum(1 for c in library if _layer_same_label(layer, c, query))
            topk = lr.recalled_ids[:K_FINAL]
            topk_same = sum(
                1 for cid in topk if (c := by_id.get(cid)) and _layer_same_label(layer, c, query)
            )
            precisions.append(topk_same / K_FINAL)
            hit_rates.append(1.0 if topk_same > 0 else 0.0)
            same_recalls.append(topk_same / total_same if total_same else 0.0)
            # MRR over the full ranked list
            rr = 0.0
            for rank, cid in enumerate(lr.recalled_ids, 1):
                c = by_id.get(cid)
                if c and _layer_same_label(layer, c, query):
                    rr = 1.0 / rank
                    break
            rrs.append(rr)
        out[layer] = {
            "precision@k": statistics.mean(precisions),
            "hitrate@k": statistics.mean(hit_rates),
            "MRR": statistics.mean(rrs),
            "same_label_recall@k": statistics.mean(same_recalls),
        }
    return out


def report_layer_metrics(metrics: dict[str, dict[str, float]]) -> None:
    """Print the per-layer aggregate metrics table."""
    print("\n" + SEP)
    print(f"层聚合指标 (k={K_FINAL}; 同标签=该层该召回)")
    print(SEP)
    for layer in ("symptom", "root_cause"):
        m = metrics[layer]
        same = "同症状" if layer == "symptom" else "同根因"
        noise = 1.0 - m["precision@k"]
        print(f"\n  [{layer} 层] 同标签={same}")
        print(f"    Precision@{K_FINAL} = {m['precision@k']:.3f}  (同标签占比)")
        print(f"    HitRate@{K_FINAL}   = {m['hitrate@k']:.3f}  (命中占比)")
        print(f"    MRR            = {m['MRR']:.3f}  (首同标签排名)")
        print(f"    同标签 Recall@{K_FINAL} = {m['same_label_recall@k']:.3f}  (k-bounded)")
        print(f"    噪声占比@{K_FINAL}  = {noise:.3f}  (1-precision)")


# ── Operational measurement (post-MMR, real thresholds) ─────────────
# The calibration pass above runs at threshold=0 + OVERFETCH=100 to collect the
# FULL cosine distribution (for threshold calibration). But MMR's top-3 there
# runs over the full ~95-pool -- including diff-symptom cases the production
# 0.60 threshold would filter out -- which would under-state precision and
# over-state diversity. This pass measures at the PRODUCTION operating point
# (real thresholds 0.60/0.61 + OVERFETCH=10 + k=3) for accurate post-MMR
# quality + diversity numbers. Reuses the already-indexed test collection.
#
# Symptom layer is run twice: λ=0.5 (MMR, production) and λ=0 (pure-score
# baseline -- MMR with λ=0 degenerates to pure rel_norm order, exact same
# ranking as pre-MMR top-k). The delta = MMR's diversity gain vs precision cost.
# Root-cause layer runs once (no MMR, pure top-k -- depth wants same-root).

OPS_K = K_FINAL  # 3


async def _ops_query_layer(cases: list[TestCase], layer: str, k: int) -> dict[str, list[str]]:
    """Query one layer for every case at k; return {query_id: [trace_id, ...]}."""
    out: dict[str, list[str]] = {}
    total = len(cases)
    for i, query in enumerate(cases, 1):
        if i == 1 or i % 20 == 0 or i == total:
            print(f"    [ops {layer}] query {i}/{total}")
        if layer == "symptom":
            scored = await search_historical_cases(_evidence(query), k_final=k)
        else:
            scored = await search_by_root_cause(
                query.root_cause_summary, k_final=k, exclude_trace_ids=[query.case_id]
            )
        out[query.case_id] = [s.payload.get("trace_id") for s in scored]
    return out


def _ops_metrics(
    per_query_topk: dict[str, list[str]], cases: list[TestCase], layer: str
) -> dict[str, float]:
    """Precision@k / HitRate@k / MRR / distinct-root-types@k for one config.

    distinct-root-types@k = how many distinct root_cause_type values appear in
    top-k (the diversity signal; higher = more root-cause directions for the LLM).
    """
    by_id = {c.case_id: c for c in cases}
    precisions: list[float] = []
    hit_rates: list[float] = []
    rrs: list[float] = []
    distinct_roots: list[int] = []
    for query in cases:
        topk = per_query_topk.get(query.case_id, [])
        same = sum(
            1 for cid in topk if (c := by_id.get(cid)) and _layer_same_label(layer, c, query)
        )
        precisions.append(same / len(topk) if topk else 0.0)
        hit_rates.append(1.0 if same > 0 else 0.0)
        roots = {by_id[cid].root_cause_type for cid in topk if cid in by_id}
        distinct_roots.append(len(roots))
        rr = 0.0
        for rank, cid in enumerate(topk, 1):
            c = by_id.get(cid)
            if c and _layer_same_label(layer, c, query):
                rr = 1.0 / rank
                break
        rrs.append(rr)
    return {
        "precision@k": statistics.mean(precisions),
        "hitrate@k": statistics.mean(hit_rates),
        "MRR": statistics.mean(rrs),
        "distinct_roots@k": statistics.mean(distinct_roots),
    }


def report_operational(
    sym_mmr: dict[str, float], sym_pure: dict[str, float], rc: dict[str, float]
) -> None:
    """Print the operational quality + diversity table + MMR-vs-pure delta."""
    print("\n" + SEP)
    print(f"Operational 模式 (阈值 0.60/0.61 + OVERFETCH=10 + k={OPS_K}, 生产操作点)")
    print(SEP)
    print(
        f"\n  {'config':<22}{'Precision@3':>13}{'HitRate@3':>11}{'MRR':>8}{'distinct-roots@3':>19}"
    )
    print(f"  {'-' * 71}")
    rows = [
        ("症状层 MMR (λ=0.5)", sym_mmr),
        ("症状层 pure (λ=0)", sym_pure),
        ("根因层 (无 MMR)", rc),
    ]
    for name, m in rows:
        print(
            f"  {name:<22}{m['precision@k']:>13.3f}{m['hitrate@k']:>11.3f}"
            f"{m['MRR']:>8.3f}{m['distinct_roots@k']:>19.3f}"
        )

    d_root = sym_mmr["distinct_roots@k"] - sym_pure["distinct_roots@k"]
    d_prec = sym_mmr["precision@k"] - sym_pure["precision@k"]
    root_dir = "↑" if d_root > 0.001 else "≈"
    prec_dir = "↑" if d_prec > 0.001 else ("↓" if d_prec < -0.001 else "≈")
    print("\n  MMR 效果 (症状层 vs pure baseline):")
    print(
        f"    distinct-roots@3: {sym_pure['distinct_roots@k']:.3f} -> "
        f"{sym_mmr['distinct_roots@k']:.3f}  (Δ{d_root:+.3f}, 多样性{root_dir})"
    )
    print(
        f"    Precision@3:      {sym_pure['precision@k']:.3f} -> "
        f"{sym_mmr['precision@k']:.3f}  (Δ{d_prec:+.3f}, precision{prec_dir})"
    )
    print(
        "  (MMR 第一个选最高分不变 -> MRR/HitRate 通常不受影响;后续位用 root_cause 向量冗余换多样)"
    )


# ── Main ─────────────────────────────────────────────────────────────


async def main() -> None:
    print(SEP)
    print("检索侧验证 (leave-one-out, 真实管线, 独立测试库)")
    print(SEP)
    print(f"  测试库: {TEST_COLLECTION} (与开发库 historical_cases 物理隔离)")
    print(
        f"  确定性: confidence={UNIFORM_CONFIDENCE} 统一 -> importance 常数, "
        f"三因子退化为纯 relevance 排序"
    )
    print(f"  fixture: {FIXTURE_PATH}")

    cases = load_cases()
    print(f"\n[1] 加载 {len(cases)} 合成 case")

    # ── isolate: point both modules at the test collection ──
    cs.COLLECTION_NAME = TEST_COLLECTION
    cr.COLLECTION_NAME = TEST_COLLECTION
    print(f"[2] 隔离: case_store/case_retriever COLLECTION_NAME -> {TEST_COLLECTION}")

    # ── embed cache: dedup redundant API embeds (9120 -> ~192 unique) ──
    cs.embed_texts = _cached_embed_texts
    cr.embed_single = _cached_embed_single
    print("[2b] embed 缓存: 脚本作用域 monkeypatch (同文本同向量,9120 -> 唯一 embed)")

    # ── calibration mode: threshold=0 + large overfetch ──
    # The placeholder thresholds (0.75, bge-m3-tuned) filter ALL v4 candidates
    # (v4 cosine分布偏低) -> empty recall + no cosine data to calibrate from.
    # Run at threshold=0 + OVERFETCH=K_CALIBRATE to collect the FULL cosine
    # distribution; recall@K_FINAL is the top-K_FINAL slice (raw ranking
    # quality). The calibrated threshold is read off the distribution below.
    cr.SYMPTOM_RELEVANCE_THRESHOLD = 0.0
    cr.ROOT_CAUSE_RELEVANCE_THRESHOLD = 0.0
    cr.OVERFETCH = K_CALIBRATE
    print(
        f"[2c] 标定模式: threshold=0 + OVERFETCH={K_CALIBRATE} "
        f"(收集全分布, recall@{K_FINAL}=top-{K_FINAL} 切片)"
    )

    print(f"\n[3] leave-one-out ({len(cases)} 轮 × 2 层 = {len(cases) * 2} 次检索)...")
    results, report = await leave_one_out(cases)

    scores = score_propositions(results, cases)
    report_propositions(scores)
    metrics = score_layer_metrics(results, cases)
    report_layer_metrics(metrics)
    report_thresholds(report)

    # ── operational measurement (post-MMR, real thresholds) ──
    print(f"\n[4] operational 测量 (真实阈值 + OVERFETCH=10 + k={OPS_K}, 复用已索引 test 库)...")
    # restore operating-point constants (calibration pass zeroed thresholds + bumped overfetch)
    cr.SYMPTOM_RELEVANCE_THRESHOLD = SYMPTOM_RELEVANCE_THRESHOLD  # 0.60
    cr.ROOT_CAUSE_RELEVANCE_THRESHOLD = ROOT_CAUSE_RELEVANCE_THRESHOLD  # 0.61
    cr.OVERFETCH = 10

    # symptom MMR (λ=0.5, production default)
    cr.MMR_LAMBDA = MMR_LAMBDA  # 0.5
    print(f"  [4a] 症状层 MMR (λ={cr.MMR_LAMBDA})...")
    sym_mmr = _ops_metrics(await _ops_query_layer(cases, "symptom", OPS_K), cases, "symptom")

    # symptom pure baseline (λ=0 -> MMR degenerates to pure score top-k = pre-MMR behavior)
    cr.MMR_LAMBDA = 0.0
    print(f"  [4b] 症状层 pure baseline (λ={cr.MMR_LAMBDA})...")
    sym_pure = _ops_metrics(await _ops_query_layer(cases, "symptom", OPS_K), cases, "symptom")

    # root_cause layer (no MMR, pure score top-k -- depth wants same-root)
    print("  [4c] 根因层 (纯 score top-k)...")
    rc_ops = _ops_metrics(await _ops_query_layer(cases, "root_cause", OPS_K), cases, "root_cause")

    report_operational(sym_mmr, sym_pure, rc_ops)

    print("\n" + SEP)
    print("结论:")
    print("  - 双向量两层职责:症状层 P3 高/P2 低(多根因思路),根因层 P2 高/P3 低(跨症状复用)")
    print("  - 阈值:按上方候选分离点回填 SYMPTOM_/ROOT_CAUSE_RELEVANCE_THRESHOLD")
    print("  - operational 模式给 post-MMR 生产操作点的 Precision/MRR/多样性(MMR vs pure delta)")
    print(SEP)


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    asyncio.run(main())
