"""Debug script: run any bug case through the Doctor diagnosis pipeline.

Usage:
    python debug_be020.py              # uses CASE_ID below
    python debug_be020.py BE-020       # override via CLI arg
    python debug_be020.py FE-020       # debug any case
"""

import asyncio
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "src")

from src.graph.main_graph import generate_thread_id, get_graph
from src.graph.state import BrowserError, DoctorState, Evidence, LogEntry, TraceSpan

# ── 改这里切换要调试的 Bug ──────────────────────────────────────────
CASE_ID = "BE-020"
# ────────────────────────────────────────────────────────────────────


async def main() -> None:
    # CLI 参数可覆盖 CASE_ID
    case_id = sys.argv[1] if len(sys.argv) > 1 else CASE_ID
    evidence_dir = Path(f"../bug-factory/output/{case_id}/evidence")

    if not evidence_dir.is_dir():
        print(f"[ERROR] Evidence dir not found: {evidence_dir}")
        sys.exit(1)

    # Load evidence files
    logs = json.loads((evidence_dir / "logs.json").read_text(encoding="utf-8"))
    traces = json.loads((evidence_dir / "traces.json").read_text(encoding="utf-8"))
    browser_errors = json.loads((evidence_dir / "browser_errors.json").read_text(encoding="utf-8"))
    print(f"Loaded: {len(logs)} logs, {len(traces)} traces, {len(browser_errors)} browser errors")

    # Load user_report from case.yaml (auto, no hardcode)
    case_yaml = evidence_dir.parent / "case.yaml"
    user_report = "（无用户报告）"
    if case_yaml.exists():
        case_data = yaml.safe_load(case_yaml.read_text(encoding="utf-8"))
        user_report = case_data.get("input", {}).get("user_report", user_report)
    print(f"Case: {case_id} | User report: {user_report[:80]}...")

    # Build Evidence
    log_entries = [LogEntry(**entry) for entry in logs]
    trace_spans = [TraceSpan(**t) for t in traces]
    browser_errs = [BrowserError(**b) for b in browser_errors]

    raw_evidence = Evidence(
        user_report=user_report,
        logs=log_entries,
        traces=trace_spans,
        browser_errors=browser_errs,
    )

    # 主图已包含 ingest_node，会自动归一化 raw_evidence → evidence
    state = DoctorState(
        raw_evidence=raw_evidence,
        case_id=case_id,
    )

    print(f"\n=== Running Graph for {case_id} ===")
    graph = get_graph()
    thread_id = generate_thread_id()
    result = await graph.ainvoke(
        state.model_dump(), config={"configurable": {"thread_id": thread_id}}
    )

    # Triage
    print("\n=== Triage Result ===")
    triage = result.get("triage")
    if triage:
        print(f"Primary: {getattr(triage, 'primary', 'N/A')}")
        scores = getattr(triage, "scores", [])
        for s in scores:
            cat = getattr(s, "category", "?")
            conf = getattr(s, "confidence", 0)
            print(f"  {cat}: {conf:.2f}")
        print(f"Cross-layer: {getattr(triage, 'cross_layer_suspected', False)}")
    else:
        print("(no triage result)")

    # Findings
    print("\n=== Specialist Findings ===")
    findings = result.get("findings", [])
    for f in findings:
        agent = getattr(f, "agent", "?")
        summary = getattr(f, "summary", "?")
        files = getattr(f, "affected_files", [])
        refs = getattr(f, "evidence_refs", [])
        fix = getattr(f, "fix_suggestion", "?")
        conf = getattr(f, "confidence", 0)
        print(f"\n--- {agent} (confidence={conf:.2f}) ---")
        print(f"  Summary: {summary[:250]}")
        print(f"  Affected files: {files}")
        print(f"  Evidence refs: {refs}")
        print(f"  Fix: {fix[:250]}")

    # Final Report
    print("\n=== Final Report ===")
    report = result.get("report")
    if report:
        print(f"Primary category: {getattr(report, 'primary_category', '?')}")
        print(f"Categories: {getattr(report, 'categories', [])}")
        print(f"Root cause: {getattr(report, 'root_cause', '?')[:300]}")
        print(f"Fix: {getattr(report, 'fix_suggestion', '?')[:300]}")
        print(f"Confidence: {getattr(report, 'confidence', 0):.2f}")
        print(f"Evidence chain: {getattr(report, 'evidence_chain', [])}")
        print(f"Early stopped: {getattr(report, 'early_stopped', False)}")
    else:
        print("(no report generated)")

    print("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
