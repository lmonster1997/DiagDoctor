"""
Standalone debug script for stepping through triage_node with BE-001 data.

Usage:
    1. Set breakpoints in src/graph/nodes/triage.py
    2. Open this file in VS Code
    3. Press F5 (or select "🔍 Debug Triage (BE-001)" in Run and Debug)

This bypasses the FastAPI server and benchmark runner,
so you can step-debug the triage logic directly.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Ensure doctor/src is on sys.path
_doctor_dir = Path(__file__).resolve().parent
if str(_doctor_dir) not in sys.path:
    sys.path.insert(0, str(_doctor_dir))

from src.graph.state import DoctorState, Evidence, LogEntry, TraceSpan  # noqa: E402


def load_be001_evidence() -> Evidence:
    """Load BE-001 logs and traces from bug-factory output."""
    evidence_dir = (
        _doctor_dir.parent / "bug-factory" / "output" / "BE-001" / "evidence"
    )

    logs: list[LogEntry] = []
    traces: list[TraceSpan] = []

    # Load logs
    logs_path = evidence_dir / "logs.json"
    if logs_path.exists():
        raw_logs = json.loads(logs_path.read_text(encoding="utf-8"))
        for item in raw_logs:
            try:
                logs.append(
                    LogEntry(
                        timestamp=item["timestamp"],
                        level=item.get("labels", {}).get("detected_level", "info"),
                        service=item.get("labels", {}).get("service_name", "unknown"),
                        message=item.get("line", ""),
                    )
                )
            except Exception:
                pass

    # Load traces
    traces_path = evidence_dir / "traces.json"
    if traces_path.exists():
        raw_traces = json.loads(traces_path.read_text(encoding="utf-8"))
        for item in raw_traces:
            try:
                traces.append(
                    TraceSpan(
                        span_id=item.get("span_id", ""),
                        name=item.get("operation_name", ""),
                        service=item.get("service_name", "unknown"),
                        start=item.get("start_time", ""),
                        duration_ms=item.get("duration_ms", 0.0),
                        status=item.get("status", "unset"),
                        attributes=item.get("attributes", {}),
                    )
                )
            except Exception:
                pass

    return Evidence(
        user_report=(
            "我打开任务列表后，等了很久页面才加载出来，"
            "每次点进去查看任务都要卡半天，感觉比之前慢了很多，"
            "翻看不同任务时也总是要等很久才能刷新。"
        ),
        logs=logs,
        traces=traces,
    )


async def main() -> None:
    from src.graph.nodes.triage import triage_node  # noqa: E402

    # 1. Build state with BE-001 evidence
    evidence = load_be001_evidence()
    state = DoctorState(evidence=evidence, case_id="BE-001")

    print("=" * 60)
    print("🔍 Debugging triage_node with BE-001 evidence")
    print(f"   Logs: {len(evidence.logs)} entries")
    print(f"   Traces: {len(evidence.traces)} spans")
    print(f"   User report: {evidence.user_report[:60]}...")
    print("=" * 60)

    # 2. Invoke triage_node — set breakpoints in triage.py!
    result = await triage_node(state)

    # 3. Print result
    print("\n📊 Triage Result:")
    print(f"   Category: {result.get('bug_category')}")
    print(f"   Findings: {result.get('findings')}")


if __name__ == "__main__":
    asyncio.run(main())
