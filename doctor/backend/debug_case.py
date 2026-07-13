"""Debug script: run any bug case through the Doctor diagnosis pipeline.

Usage:
    python debug_be020.py              # uses CASE_ID below
    python debug_be020.py BE-020       # override via CLI arg
    python debug_be020.py FE-020       # debug any case
"""

import asyncio
import json
import sys
import typing
from pathlib import Path

import yaml

sys.path.insert(0, "src")

from src.graph.copilotkit_graph import generate_thread_id, get_copilotkit_graph
from src.graph.state import Evidence, LogEntry, TraceSpan

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
    print(f"Loaded: {len(logs)} logs, {len(traces)} traces")

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
    raw_evidence = Evidence(
        user_report=user_report,
        logs=log_entries,
        traces=trace_spans,
    )

    # Build state dict (same as REST API _build_initial_state)
    thread_id = generate_thread_id()
    state: dict[str, typing.Any] = {
        "raw_evidence": raw_evidence,
        "case_id": case_id,
        "trace_id": thread_id,
        "session_id": thread_id,
    }

    print(f"\n=== Running Graph for {case_id} ===")
    graph = get_copilotkit_graph()
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})

    # Evidence (from bug_info node)
    print("\n=== Normalized Evidence ===")
    evidence = result.get("evidence")
    if evidence:
        print(f"golden_signals: {len(evidence.golden_signals)}")
        print(f"correlations:   {len(evidence.correlations)}")
        for sig in evidence.golden_signals:
            tier = getattr(sig, "service_tier", "?")
            sev = getattr(sig, "severity", "?")
            stype = getattr(sig, "signal_type", "?")
            summary = getattr(sig, "summary", "?")
            print(f"  [{tier}] [{sev}] [{stype}] {summary[:200]}")

    # Findings
    print("\n=== Findings ===")
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
        print(f"Symptom tier: {getattr(report, 'symptom_tier', '?')}")
        print(f"Root cause tier: {getattr(report, 'root_cause_tier', '?')}")
        print(f"Affected file: {getattr(report, 'affected_file', None)}")
        print(f"Evidence chain: {getattr(report, 'evidence_chain', [])}")
        print(f"Early stopped: {getattr(report, 'early_stopped', False)}")
    else:
        print("(no report generated)")

    # Budget
    budget = result.get("budget")
    if budget:
        print(
            f"\n[Budget] tool_calls={getattr(budget, 'tool_calls', 0)}, "
            f"tokens={getattr(budget, 'total_tokens', 0)}, "
            f"elapsed={getattr(budget, 'elapsed_seconds', 0):.1f}s"
        )

    print("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
