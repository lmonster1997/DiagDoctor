"""Recall ablation eval (design §9.1/§9.3) - P0 symptom-cosine recall per quadrant.

Loads the 15 bug-factory cases, ingests each case's evidence into
``NormalizedEvidence``, embeds the symptom passage (``build_symptom_passage``),
computes the pairwise symptom-cosine matrix, and reports P0 recall@k per
quadrant (same/diff root-cause × same/diff symptom).

Demonstrates P0's symptom-similarity ceiling:
- same-root + diff-symptom  -> LOW recall (the ceiling P1-a breaks)
- diff-root + same-symptom  -> HIGH recall (over-recall P1-a fixes)
This is the "before" baseline; P1-a's ``root_cause_vector`` re-runs the same
quadrants by root-cause cosine for the before/after ablation.

Integration: needs ``embed_single`` (TEI or local bge-m3). No Qdrant, no full
doctor run (in-memory cosine over the 15 cases' pre-collected evidence).

Usage::

    cd doctor/backend && uv run python scripts/eval_recall_ablation.py
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
    build_rankings,
    format_quadrant_report,
    recall_at_k_per_quadrant,
)

PROJECT_ROOT = DOCTOR_BACKEND.parent.parent  # DiagDoctor/
BUG_FACTORY_OUTPUT = PROJECT_ROOT / "bug-factory" / "output"

K_VALUES = (3, 5)

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


# ── Evidence loading + format conversion (mirrors benchmark runner) ───


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

    Character-bigram hashing: passages sharing bigrams (e.g. the symptom anchor
    ``信号: slow_span | 层级: backend``) get higher cosine. It approximates
    *lexical* symptom similarity, so same-symptom cases tend to rank together -
    enough to demonstrate the four-quadrant mechanism + the P0 ceiling pattern
    without the real bge-m3 model. NOT the real embedding - real numbers need
    TEI or a cached bge-m3 (``embed_single``).
    """
    vec: list[float] = [0.0] * _MOCK_DIM
    for i in range(max(0, len(text) - 1)):
        bg = text[i : i + 2]
        h = int(hashlib.md5(bg.encode("utf-8")).hexdigest(), 16) % _MOCK_DIM
        vec[h] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else vec


# ── Main ─────────────────────────────────────────────────────────────


async def main(mock_embed: bool = False) -> None:
    print("=" * 60)
    print("  Recall ablation (P0 symptom-cosine) over 15 gold cases")
    if mock_embed:
        mode = "MOCK embed (content-based bigram hash; pipeline verify only)"
    else:
        mode = "REAL bge-m3 embed"
    print(f"  embed mode: {mode}")
    print("=" * 60)

    passages: dict[str, str] = {}
    vectors: dict[str, list[float]] = {}
    labels: dict[str, CaseLabel] = {}

    for case_id in CASE_IDS:
        evidence = _load_case_evidence(case_id)
        if evidence is None:
            continue
        passage = build_symptom_passage(evidence)
        try:
            vec = _mock_embed(passage) if mock_embed else await embed_single(passage)
        except Exception as exc:
            print(f"  [SKIP] {case_id}: embed failed: {exc}")
            continue
        passages[case_id] = passage
        vectors[case_id] = vec
        labels[case_id] = CaseLabel(
            case_id=case_id,
            root_cause_type=ROOT_CAUSE_TYPE[case_id],
            symptom_type=SYMPTOM_TYPE[case_id],
        )
        print(
            f"  [OK] {case_id}: root={ROOT_CAUSE_TYPE[case_id]} "
            f"symptom={SYMPTOM_TYPE[case_id]} "
            f"ingest={_symptom_type(evidence)} signals={len(evidence.golden_signals)}"
        )

    if len(vectors) < 2:
        print("\n[FAIL] Need ≥2 embedded cases. Is TEI/local bge-m3 available?")
        return

    # Pairwise symptom-cosine relevance matrix.
    relevance: dict[str, dict[str, float]] = {}
    for q in vectors:
        relevance[q] = {c: _cosine(vectors[q], vectors[c]) for c in vectors if c != q}

    rankings = build_rankings(relevance)

    print(f"\n{'=' * 60}")
    print(f"  Embedded {len(vectors)} cases. Symptom-type distribution:")
    sym_counts: dict[str, int] = {}
    root_counts: dict[str, int] = {}
    for lbl in labels.values():
        sym_counts[lbl.symptom_type] = sym_counts.get(lbl.symptom_type, 0) + 1
        root_counts[lbl.root_cause_type] = root_counts.get(lbl.root_cause_type, 0) + 1
    print(f"  root_cause_type: {root_counts}")
    print(f"  symptom_type:    {sym_counts}")

    for k in K_VALUES:
        print()
        results = recall_at_k_per_quadrant(rankings, labels, k)
        print(format_quadrant_report(results, k))

    print(f"\n{'=' * 60}")
    print("  Interpretation (P0 ceiling):")
    print("  - same_root_diff_symptom LOW  = P0 misses same-root-diff-symptom (ceiling)")
    print("  - diff_root_same_symptom HIGH = P0 over-recalls diff-root-same-symptom")
    print("  -> P1-a (root_cause_vector) flips both: the before/after ablation.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Recall ablation eval (§9.1/§9.3)")
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
    asyncio.run(main(mock_embed=args.mock_embed))
