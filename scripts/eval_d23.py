"""
D23 验收脚本 — 跑 BE-020 / PERF-020 等 case 验证 Backend Specialist 质量。

验收标准（执行手册 §22.3）：
- BE-020 / PERF-020 类型 case 的 finding 不为空、含 affected_files 且 evidence_refs 非空
- 门禁指标 overall ≥ 0.55
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Ensure doctor/src is on path
DOCTOR_SRC = Path(__file__).resolve().parent.parent / "doctor" / "src"
sys.path.insert(0, str(DOCTOR_SRC))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUG_FACTORY_OUTPUT = PROJECT_ROOT / "bug-factory" / "output"

from src.graph.main_graph import generate_thread_id, get_graph  # noqa: E402
from src.graph.state import (  # noqa: E402
    BrowserError,
    DiagnosisReport,
    Evidence,
    Finding,
    LogEntry,
    TraceSpan,
)


# ── Helpers ──────────────────────────────────────────────────────────


def load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def load_case(case_id: str) -> tuple[Evidence, dict[str, Any]]:
    """Load raw evidence and expected data for a case."""
    case_dir = BUG_FACTORY_OUTPUT / case_id
    evidence_dir = case_dir / "evidence"

    import yaml

    case_yaml = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    expected = case_yaml.get("expected", {})

    logs_raw = load_json(evidence_dir / "logs.json")
    traces_raw = load_json(evidence_dir / "traces.json")
    browser_raw = load_json(evidence_dir / "browser_errors.json")

    evidence = Evidence(
        user_report=case_yaml.get("input", {}).get("user_report", ""),
        logs=[LogEntry(**item) for item in logs_raw],
        traces=[TraceSpan(**item) for item in traces_raw],
        browser_errors=[BrowserError(**item) for item in browser_raw],
    )
    return evidence, expected


# ── Scoring helpers ──────────────────────────────────────────────────


def evaluate_case(
    report: DiagnosisReport | None,
    findings: list[Finding],
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Score a single case against expected diagnosis."""
    scores: dict[str, float] = {}
    details: list[str] = []

    # ── 1. Finding 不为空 ──
    specialist_findings = [f for f in findings if f.agent != "TriageAgent"]
    finding_not_empty = len(specialist_findings) > 0 and bool(specialist_findings[0].summary)
    scores["finding_not_empty"] = 1.0 if finding_not_empty else 0.0
    if not finding_not_empty:
        details.append("❌ 无 specialist finding")

    # ── 2. 含 affected_files ──
    has_affected = any(len(f.affected_files) > 0 for f in specialist_findings)
    scores["has_affected_files"] = 1.0 if has_affected else 0.0
    if not has_affected:
        details.append("❌ finding 缺少 affected_files")

    # ── 3. evidence_refs 非空 ──
    has_refs = any(len(f.evidence_refs) > 0 for f in specialist_findings)
    scores["has_evidence_refs"] = 1.0 if has_refs else 0.0
    if not has_refs:
        details.append("❌ finding 缺少 evidence_refs")

    # ── 4. Primary category correctness ──
    expected_category = expected.get("category", "")
    if report and report.primary_category == expected_category:
        scores["category_correct"] = 1.0
    elif report:
        scores["category_correct"] = 0.5  # partial: at least something was classified
        details.append(
            f"⚠️ primary_category mismatch: got='{report.primary_category}', expected='{expected_category}'"
        )

    # ── 5. Confidence ──
    if report:
        scores["confidence"] = min(report.confidence, 1.0)
    else:
        scores["confidence"] = 0.0

    # ── 6. Affected file hits ──
    expected_files = expected.get("affected_files", [])
    all_found_files: set[str] = set()
    for f in specialist_findings:
        all_found_files.update(f.affected_files)
    if expected_files and all_found_files:
        hits = sum(
            1 for ef in expected_files if any(ef in aff or aff in ef for aff in all_found_files)
        )
        scores["file_localization"] = hits / len(expected_files)
    else:
        scores["file_localization"] = 0.0

    # ── Aggregate score ──
    weights = {
        "finding_not_empty": 0.20,
        "has_affected_files": 0.15,
        "has_evidence_refs": 0.15,
        "category_correct": 0.25,
        "confidence": 0.10,
        "file_localization": 0.15,
    }
    overall = sum(scores.get(k, 0.0) * w for k, w in weights.items())

    return {
        "scores": scores,
        "overall": overall,
        "details": details,
        "findings_summary": [f.summary[:150] for f in specialist_findings],
        "affected_files": list(all_found_files),
        "evidence_refs": [r for f in specialist_findings for r in f.evidence_refs],
    }


# ── Main ──────────────────────────────────────────────────────────────


async def run_case(case_id: str) -> dict[str, Any]:
    """Run a single case through the doctor graph."""
    print(f"\n{'=' * 60}")
    print(f"  Running case: {case_id}")
    print(f"{'=' * 60}")

    evidence, expected = load_case(case_id)

    print(f"  User report: {evidence.user_report[:100]}...")
    print(
        f"  Logs: {len(evidence.logs)}, Traces: {len(evidence.traces)}, "
        f"Browser errors: {len(evidence.browser_errors or [])}"
    )
    print(f"  Expected category: {expected.get('category')}")

    # Build initial state
    thread_id = generate_thread_id()
    initial_state: dict[str, Any] = {
        "raw_evidence": evidence,
        "case_id": case_id,
        "trace_id": thread_id,
        "session_id": thread_id,
    }

    # Invoke graph
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    print(f"  Invoking graph (thread={thread_id})...")
    try:
        result = await graph.ainvoke(initial_state, config)
    except Exception as exc:
        print(f"  ❌ Graph invocation failed: {exc}")
        return {
            "case_id": case_id,
            "error": str(exc),
            "overall": 0.0,
        }

    # Extract results
    report: DiagnosisReport | None = result.get("report")
    findings: list[Finding] = result.get("findings", [])

    print(f"  Report primary: {report.primary_category if report else 'N/A'}")
    print(f"  Report confidence: {report.confidence:.2f}%" if report else "  No report")
    print(f"  Total findings: {len(findings)}")

    for f in findings:
        print(f"    [{f.agent}] conf={f.confidence:.2f}: {f.summary[:120]}...")

    # Evaluate
    eval_result = evaluate_case(report, findings, expected)
    print("\n  ── Scores ──")
    for k, v in eval_result["scores"].items():
        print(f"    {k}: {v:.2f}")
    print(f"    OVERALL: {eval_result['overall']:.2f}")

    for detail in eval_result["details"]:
        print(f"  {detail}")

    return {
        "case_id": case_id,
        "overall": eval_result["overall"],
        "scores": eval_result["scores"],
        **eval_result,
    }


async def main() -> None:
    # Reset graph cache to pick up latest code
    import src.graph.main_graph as mg

    mg._graph_instance = None  # type: ignore[attr-defined]

    cases_to_run = ["BE-020"]
    # Also try PERF-020 if it has evidence
    perf_dir = BUG_FACTORY_OUTPUT / "PERF-020" / "case.yaml"
    if perf_dir.exists():
        cases_to_run.append("PERF-020")

    print("=" * 60)
    print("  DiagDoctor D23 验收评测")
    print(f"  Cases: {cases_to_run}")
    print("=" * 60)

    results = []
    for case_id in cases_to_run:
        result = await run_case(case_id)
        results.append(result)

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    overalls = [r["overall"] for r in results if "overall" in r]
    if overalls:
        avg_overall = sum(overalls) / len(overalls)
        print(f"  Average overall: {avg_overall:.2f}")
        print(f"  Cases: {len(overalls)}")

        # D23 acceptance: overall ≥ 0.55
        if avg_overall >= 0.55:
            print(f"\n  ✅ D23 验收通过！ overall={avg_overall:.2f} ≥ 0.55")
        else:
            print(f"\n  ❌ D23 验收未通过：overall={avg_overall:.2f} < 0.55")

    # Check individual case acceptance criteria
    print("\n  ── 逐 case 验收项 ──")
    for r in results:
        case_id = r.get("case_id", "?")
        scores = r.get("scores", {})
        finding_ok = scores.get("finding_not_empty", 0) >= 1.0
        affected_ok = scores.get("has_affected_files", 0) >= 1.0
        refs_ok = scores.get("has_evidence_refs", 0) >= 1.0
        all_ok = finding_ok and affected_ok and refs_ok

        status = "✅" if all_ok else "❌"
        print(
            f"  {status} {case_id}: finding非空={finding_ok}, "
            f"affected_files={affected_ok}, evidence_refs={refs_ok}"
        )


if __name__ == "__main__":
    asyncio.run(main())
