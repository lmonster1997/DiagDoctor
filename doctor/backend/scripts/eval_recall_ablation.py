"""Recall ablation eval (design §9.1/§9.3) - P1-a before/after dual-vector ablation.

Loads the 15 bug-factory cases and computes pairwise-cosine recall@k per
quadrant (same/diff root-cause × same/diff symptom), under either vector:

- ``--vector symptom``   (P0 "before"): embed each case's symptom passage
  (``build_symptom_passage``) -> symptom-cosine. Demonstrates P0's symptom-
  similarity ceiling.
- ``--vector root_cause`` (P1-a "after"): embed each case's
  ``expected.root_cause_summary`` -> root_cause-cosine. The named vector the
  agent's ``search_historical_root_cause`` tool queries.
- ``--vector both``       (default ablation): runs before + after and prints a
  side-by-side delta table. The breakthrough = quadrant ② (same-root-diff-
  symptom) recall UP + quadrant ③ (diff-root-same-symptom) recall DOWN.

P0 ceiling (the "before" story):
- same-root + diff-symptom  -> LOW recall (the ceiling P1-a breaks)
- diff-root + same-symptom  -> HIGH recall (over-recall P1-a fixes)
P1-a flips both: same-root -> root_cause text similar -> recalled;
diff-root -> root_cause text dissimilar -> not recalled.

Integration: needs ``embed_single`` (TEI or local bge-m3). No Qdrant, no full
doctor run (in-memory cosine over the 15 cases' pre-collected evidence /
root_cause text).

Usage::

    cd doctor/backend && uv run python scripts/eval_recall_ablation.py            # both (before/after ablation)
    cd doctor/backend && uv run python scripts/eval_recall_ablation.py --vector symptom
    cd doctor/backend && uv run python scripts/eval_recall_ablation.py --vector root_cause --mock-embed
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# Add doctor/backend to sys.path so `src.*` imports resolve when run as a script.
DOCTOR_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DOCTOR_BACKEND))

from src.engine.state import NormalizedEvidence  # noqa: E402
from src.evidence.normalizer import ingest  # noqa: E402
from src.memory.long_term.embedding import embed_single  # noqa: E402
from src.memory.long_term.encoding import build_symptom_passage, derive_tier  # noqa: E402
from src.memory.long_term.recall_ablation import (  # noqa: E402
    CaseLabel,
    Q_DIFF_ROOT_DIFF_SYM,
    Q_DIFF_ROOT_SAME_SYM,
    Q_SAME_ROOT_DIFF_SYM,
    Q_SAME_ROOT_SAME_SYM,
    QUADRANT_DESCRIPTIONS,
    build_rankings,
    format_quadrant_report,
    recall_at_k_per_quadrant,
)

PROJECT_ROOT = DOCTOR_BACKEND.parent.parent  # DiagDoctor/
BUG_FACTORY_OUTPUT = PROJECT_ROOT / "bug-factory" / "output"

K_VALUES = (3, 5)

# Quadrant display order + the two ceiling-breaker quadrants P1-a targets.
_QUADRANT_ORDER = (
    Q_SAME_ROOT_SAME_SYM,
    Q_SAME_ROOT_DIFF_SYM,
    Q_DIFF_ROOT_SAME_SYM,
    Q_DIFF_ROOT_DIFF_SYM,
)

# Manual root-cause-type per case. Cases sharing a type have the same underlying
# root-cause PATTERN (e.g. PERF-020/021 both N+1; BE-022/FE-021 both
# null-assignee-unhandled). Derived from each recipe's root_cause.
ROOT_CAUSE_TYPE: dict[str, str] = {
    "BE-020": "missing-fk-check",
    "BE-021": "scalar-type-error",
    "BE-022": "null-check",
    "FE-020": "missing-field",
    "FE-021": "null-check",
    "PERF-020": "n-plus-1",
    "PERF-021": "n-plus-1",
    "LOGIC-020": "idor",
    "LOGIC-021": "idor",
    "LOGIC-022": "silent-data-loss",
    "DATA-020": "sort-logic",
    "DATA-021": "silent-data-loss",
    "RACE-020": "race",
    "CONFIG-020": "config",
    "CASCADE-020": "cascade",
}

# Manual symptom-type per case (the discriminative manifestation). The ingest's
# signal_types are too coarse (most cases extract generic error_log+slow_span),
# so the symptom is labeled manually from each bug's actual manifestation.
SYMPTOM_TYPE: dict[str, str] = {
    "BE-020": "http_500",  # FK IntegrityError -> 500
    "BE-021": "http_500",  # NoResultFound -> 500
    "BE-022": "http_500",  # AttributeError -> 500
    "FE-020": "frontend_crash",  # undefined tags -> 白屏
    "FE-021": "frontend_crash",  # null assignee -> 白屏
    "PERF-020": "slow_query",  # N+1
    "PERF-021": "slow_query",  # N+1
    "LOGIC-020": "access_control_anomaly",  # IDOR get
    "LOGIC-021": "access_control_anomaly",  # IDOR list leak
    "LOGIC-022": "silent_data_loss",  # status drop
    "DATA-020": "wrong_sort",  # sort order
    "DATA-021": "silent_data_loss",  # due_date drop
    "RACE-020": "intermittent_error",  # race
    "CONFIG-020": "misconfig",  # jwt expiry
    "CASCADE-020": "cascade_failure",  # retry storm
}

CASE_IDS = list(ROOT_CAUSE_TYPE.keys())

VECTOR_MODES = ("symptom", "root_cause", "both")


# ── Evidence / text loading (mirrors benchmark runner) ───────────────


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _convert_log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """bug-factory LogEntry -> doctor LogEntry format (mirrors runner.py)."""
    labels: dict[str, str] = entry.get("labels", {})
    return {
        "timestamp": entry.get("timestamp", ""),
        "level": labels.get("detected_level", "unknown"),
        "service": labels.get("service_name", "unknown"),
        "message": entry.get("line", ""),
        "trace_id": labels.get("trace_id"),
        "attributes": {
            k: v
            for k, v in labels.items()
            if k not in ("detected_level", "service_name", "trace_id")
        },
    }


def _convert_trace_span(span: dict[str, Any]) -> dict[str, Any]:
    """bug-factory TraceSpan -> doctor TraceSpan format (mirrors runner.py)."""
    attrs: dict[str, str] = span.get("attributes", {})
    return {
        "span_id": span.get("span_id", ""),
        "parent_id": attrs.get("parent_id"),
        "name": span.get("operation_name", span.get("name", "")),
        "service": span.get("service_name", ""),
        "start": span.get("start_time", ""),
        "duration_ms": span.get("duration_ms", 0.0),
        "attributes": attrs,
        "status": span.get("status", "ok"),
    }


def _load_case_evidence(case_id: str) -> NormalizedEvidence | None:
    """Load a case's user_report + logs/traces -> ingest -> NormalizedEvidence."""
    case_dir = BUG_FACTORY_OUTPUT / case_id
    case_yaml = case_dir / "case.yaml"
    if not case_yaml.is_file():
        print(f"  [SKIP] {case_id}: case.yaml not found")
        return None

    raw_case = yaml.safe_load(case_yaml.read_text(encoding="utf-8")) or {}
    user_report = str((raw_case.get("input") or {}).get("user_report") or "")

    evidence_dir = case_dir / "evidence"
    logs = [_convert_log_entry(e) for e in _load_json(evidence_dir / "logs.json")]
    traces = [_convert_trace_span(s) for s in _load_json(evidence_dir / "traces.json")]

    return ingest({"user_report": user_report, "logs": logs, "traces": traces})


def _load_case_root_cause(case_id: str) -> str | None:
    """Load a case's gold root-cause text (``expected.root_cause_summary``).

    This is what the index side (``case_store``) embeds into the ``root_cause``
    named vector in production (``report.root_cause``); the eval uses the gold
    text so the ablation is faithful to the live dual-vector retrieval.
    """
    case_yaml = BUG_FACTORY_OUTPUT / case_id / "case.yaml"
    if not case_yaml.is_file():
        return None
    try:
        raw = yaml.safe_load(case_yaml.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    text = str((raw.get("expected") or {}).get("root_cause_summary") or "")
    return text or None


def _symptom_type(evidence: NormalizedEvidence) -> str:
    """Canonical symptom type: ``tier:{sorted signal_types}``."""
    tier = derive_tier(evidence)
    sigs = sorted({s.signal_type for s in evidence.golden_signals})
    return f"{tier}:{{{','.join(sigs)}}}" if sigs else f"{tier}:{{}}"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


_MOCK_DIM = 256


def _mock_embed(text: str) -> list[float]:
    """Deterministic content-based stand-in for bge-m3 (no network needed).

    Character-bigram hashing: passages sharing bigrams get higher cosine. It
    approximates *lexical* similarity, so same-root cases (whose root_cause
    texts share mechanism words, e.g. "assignee_id"/"N+1") tend to rank together
    under root_cause mode, and same-symptom cases under symptom mode -- enough to
    demonstrate the four-quadrant mechanism + the ceiling-flip pattern without
    the real bge-m3 model. NOT the real embedding - real numbers need TEI or a
    cached bge-m3 (``embed_single``).
    """
    vec: list[float] = [0.0] * _MOCK_DIM
    for i in range(max(0, len(text) - 1)):
        bg = text[i : i + 2]
        h = int(hashlib.md5(bg.encode("utf-8")).hexdigest(), 16) % _MOCK_DIM
        vec[h] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else vec


# ── Embedding + quadrant computation per vector mode ─────────────────


async def _embed_cases(
    mock_embed: bool, vector_mode: str
) -> tuple[dict[str, list[float]], dict[str, CaseLabel]]:
    """Load + embed each case's text for ``vector_mode``.

    ``symptom`` -> ``build_symptom_passage(evidence)`` (P0 before).
    ``root_cause`` -> ``expected.root_cause_summary`` (P1-a after).
    Returns (case_id -> vector, case_id -> CaseLabel).
    """
    vectors: dict[str, list[float]] = {}
    labels: dict[str, CaseLabel] = {}

    for case_id in CASE_IDS:
        if vector_mode == "root_cause":
            text = _load_case_root_cause(case_id)
            evidence = None
        else:  # symptom
            evidence = _load_case_evidence(case_id)
            if evidence is None:
                continue
            text = build_symptom_passage(evidence)

        if not text:
            print(f"  [SKIP] {case_id}: no {vector_mode} text")
            continue
        try:
            vec = _mock_embed(text) if mock_embed else await embed_single(text)
        except Exception as exc:
            print(f"  [SKIP] {case_id}: embed failed: {exc}")
            continue

        vectors[case_id] = vec
        labels[case_id] = CaseLabel(
            case_id=case_id,
            root_cause_type=ROOT_CAUSE_TYPE[case_id],
            symptom_type=SYMPTOM_TYPE[case_id],
        )
        if vector_mode == "root_cause":
            preview = text[:50].replace("\n", " ")
            print(
                f"  [OK] {case_id}: root={ROOT_CAUSE_TYPE[case_id]} "
                f"symptom={SYMPTOM_TYPE[case_id]} rc={preview!r}"
            )
        else:
            assert evidence is not None
            print(
                f"  [OK] {case_id}: root={ROOT_CAUSE_TYPE[case_id]} "
                f"symptom={SYMPTOM_TYPE[case_id]} "
                f"ingest={_symptom_type(evidence)} signals={len(evidence.golden_signals)}"
            )

    return vectors, labels


def _quadrant_results(
    vectors: dict[str, list[float]], labels: dict[str, CaseLabel], k: int
) -> list:
    """Build cosine matrix + rankings + per-quadrant recall@k."""
    relevance: dict[str, dict[str, float]] = {
        q: {c: _cosine(vectors[q], vectors[c]) for c in vectors if c != q} for q in vectors
    }
    rankings = build_rankings(relevance)
    return recall_at_k_per_quadrant(rankings, labels, k)


def _print_distribution(labels: dict[str, CaseLabel]) -> None:
    sym_counts: dict[str, int] = {}
    root_counts: dict[str, int] = {}
    for lbl in labels.values():
        sym_counts[lbl.symptom_type] = sym_counts.get(lbl.symptom_type, 0) + 1
        root_counts[lbl.root_cause_type] = root_counts.get(lbl.root_cause_type, 0) + 1
    print(f"  root_cause_type: {root_counts}")
    print(f"  symptom_type:    {sym_counts}")


def _recall_map(results: list) -> dict[str, float]:
    return {r.quadrant: r.recall_at_k for r in results}


def _print_comparison(
    before: dict[int, list], after: dict[int, list]
) -> None:
    """before/after delta table (P0 symptom -> P1-a root_cause) per k.

    The ablation verdict: ② same_root_diff_symptom should go UP (root-cause
    vector recalls same-root cases P0 missed), ③ diff_root_same_symptom should
    go DOWN (root-cause vector distinguishes different roots P0 conflated).
    """
    for k in K_VALUES:
        b = _recall_map(before[k])
        a = _recall_map(after[k])
        print(f"\n{'=' * 72}")
        print(f"  before/after ablation @ recall@{k}  (P0 symptom  ->  P1-a root_cause)")
        print(f"{'=' * 72}")
        print(f"  {'象限':<30} {'P0(symptom)':>14} {'P1-a(root_cause)':>18} {'Δ':>9}")
        print(f"  {'-' * 30} {'-' * 14} {'-' * 18} {'-' * 9}")
        for q in _QUADRANT_ORDER:
            bv, av = b.get(q, 0.0), a.get(q, 0.0)
            delta = av - bv
            sign = "+" if delta >= 0 else ""
            desc = QUADRANT_DESCRIPTIONS.get(q, q).split(" - ")[0]
            print(f"  {desc:<30} {bv:>14.2f} {av:>18.2f} {sign}{delta:>8.2f}")
        # Verdict on the two ceiling-breaker quadrants.
        d2 = a.get(Q_SAME_ROOT_DIFF_SYM, 0.0) - b.get(Q_SAME_ROOT_DIFF_SYM, 0.0)
        d3 = a.get(Q_DIFF_ROOT_SAME_SYM, 0.0) - b.get(Q_DIFF_ROOT_SAME_SYM, 0.0)
        v2 = "✓ 突破天花板" if d2 > 0 else "✗ 未升"
        v3 = "✓ 区分根因" if d3 < 0 else "✗ 未降"
        print(
            f"\n  ② same_root_diff_symptom: {b.get(Q_SAME_ROOT_DIFF_SYM, 0.0):.2f} -> "
            f"{a.get(Q_SAME_ROOT_DIFF_SYM, 0.0):.2f}  (期望升, {v2})"
        )
        print(
            f"  ③ diff_root_same_symptom: {b.get(Q_DIFF_ROOT_SAME_SYM, 0.0):.2f} -> "
            f"{a.get(Q_DIFF_ROOT_SAME_SYM, 0.0):.2f}  (期望降, {v3})"
        )
        # Honest verdict: ② is the primary win (same root cause recalled across
        # differing symptoms -- exactly what the symptom vector missed). ③ not
        # dropping is a real limitation, not a calibration gap: root-cause TEXT
        # similarity clusters by "root-cause area" (e.g. the three backend-500
        # regressions BE-020/021/022 share text semantics despite different
        # mechanical roots), which is coarser than mechanical-root identity.
        if d2 > 0:
            print("  => ② 突破:根因向量召回同根因异症状 case(P0 症状天花板被打破)✓")
        else:
            print("  => ② 未升:P1-a 未达成主目标,需复查 root_cause 文本/向量")
        if d3 < 0:
            print("  => ③ 区分:根因向量降低异根因同症状过召回 ✓")
        else:
            print(
                "  => ③ 未降(已知限制):根因文本相似按'根因领域'聚类(如后端 500 三连 "
                "BE-020/021/022 文本语义相近),粗于机械根因身份;同领域异根因仍相似。"
                "区分需结构化信号,非纯文本向量能解。"
            )


def _print_interpretation(vector_mode: str) -> None:
    print(f"\n{'=' * 60}")
    if vector_mode == "root_cause":
        print("  Interpretation (P1-a root_cause vector):")
        print("  - same_root_diff_symptom should be HIGH (root-cause text similar)")
        print("  - diff_root_same_symptom should be LOW  (root-cause text dissimilar)")
        print("  -> compare to `--vector symptom` (P0 ceiling) for the ablation.")
    else:  # symptom
        print("  Interpretation (P0 ceiling):")
        print("  - same_root_diff_symptom LOW  = P0 misses same-root-diff-symptom (ceiling)")
        print("  - diff_root_same_symptom HIGH = P0 over-recalls diff-root-same-symptom")
        print("  -> P1-a (root_cause_vector) flips both: run `--vector both` for the ablation.")
    print(f"{'=' * 60}")


# ── Main ─────────────────────────────────────────────────────────────


async def main(mock_embed: bool, vector_mode: str) -> None:
    print("=" * 60)
    title = {
        "symptom": "P0 symptom-cosine recall (before)",
        "root_cause": "P1-a root_cause-cosine recall (after)",
        "both": "P1-a dual-vector before/after ablation",
    }[vector_mode]
    print(f"  {title} over 15 gold cases")
    mode = "MOCK embed (content-based bigram hash; pipeline verify only)" if mock_embed else "REAL bge-m3 embed"
    print(f"  embed mode: {mode}")
    print("=" * 60)

    if vector_mode == "both":
        # ── before: symptom (P0) ──
        print("\n[before] P0 symptom vector:")
        sv, sl = await _embed_cases(mock_embed, "symptom")
        if len(sv) < 2:
            print("\n[FAIL] Need ≥2 embedded cases for symptom mode.")
            return
        print(f"\n  Embedded {len(sv)} cases.")
        _print_distribution(sl)
        before = {k: _quadrant_results(sv, sl, k) for k in K_VALUES}
        for k in K_VALUES:
            print()
            print(format_quadrant_report(before[k], k, title="P0 symptom-cosine recall (before)"))

        # ── after: root_cause (P1-a) ──
        print("\n[after] P1-a root_cause vector:")
        rv, rl = await _embed_cases(mock_embed, "root_cause")
        if len(rv) < 2:
            print("\n[FAIL] Need ≥2 embedded cases for root_cause mode.")
            return
        print(f"\n  Embedded {len(rv)} cases.")
        _print_distribution(rl)
        after = {k: _quadrant_results(rv, rl, k) for k in K_VALUES}
        for k in K_VALUES:
            print()
            print(format_quadrant_report(after[k], k, title="P1-a root_cause-cosine recall (after)"))

        # ── before/after delta ──
        _print_comparison(before, after)
        return

    # ── single-mode (symptom or root_cause) ──
    vectors, labels = await _embed_cases(mock_embed, vector_mode)
    if len(vectors) < 2:
        print("\n[FAIL] Need ≥2 embedded cases. Is TEI/local bge-m3 available?")
        return

    print(f"\n{'=' * 60}")
    print(f"  Embedded {len(vectors)} cases. Type distribution:")
    _print_distribution(labels)

    report_title = {
        "symptom": "P0 symptom-cosine recall",
        "root_cause": "P1-a root_cause-cosine recall",
    }[vector_mode]
    for k in K_VALUES:
        print()
        results = _quadrant_results(vectors, labels, k)
        print(format_quadrant_report(results, k, title=report_title))

    _print_interpretation(vector_mode)


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Recall ablation eval (§9.1/§9.3) - P1-a dual-vector")
    parser.add_argument(
        "--vector",
        choices=VECTOR_MODES,
        default="both",
        help=(
            "symptom=P0 before / root_cause=P1-a after / both=before+after 对比表(默认)"
        ),
    )
    parser.add_argument(
        "--mock-embed",
        action="store_true",
        help=(
            "用 mock embedder(无网络,内容 bigram 哈希)代替 bge-m3;"
            "验证管线 + 四象限机制用,数值非真实"
        ),
    )
    args = parser.parse_args()
    # Windows GBK console can't encode ✓/✗/部分中文; force utf-8 stdout.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    asyncio.run(main(mock_embed=args.mock_embed, vector_mode=args.vector))
