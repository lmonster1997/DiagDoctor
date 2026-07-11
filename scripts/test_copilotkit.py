"""Test CopilotKit 诊断路径：注入 Bug → 触发 → 聊天式诊断。

用法:
    # 全流程（注入 + 触发 + 诊断）
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020

    # 只诊断（跳过注入+触发，Bug 已在代码中）
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py --skip-inject --user-report "创建评论返回500错误"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # DiagDoctor/
DOCTOR_BACKEND = PROJECT_ROOT / "doctor" / "backend"
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"
sys.path.insert(0, str(DOCTOR_BACKEND))

PYTHON = sys.executable
DEMO_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"
RELOAD_WAIT = 5


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def run_cmd(cmd: list[str]) -> None:
    print(f"  > {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def inject_bug(recipe_id: str) -> None:
    run_cmd([PYTHON, "-m", "bug_factory.cli", "inject", recipe_id, "--in-place"])


def trigger_bug(recipe_id: str) -> tuple[str, list[str]]:
    """返回 (trigger_time_iso, trace_ids)."""
    result = subprocess.run(
        [PYTHON, "-m", "bug_factory.cli", "trigger", recipe_id, "--base-url", DEMO_URL],
        cwd=str(BUG_FACTORY_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    trigger_time = ""
    trace_ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("TRACE_IDS_JSON="):
            payload = json.loads(line[len("TRACE_IDS_JSON="):])
            trace_ids = list(payload.get("trace_ids", []))
        if line.startswith("Trigger time:"):
            trigger_time = line.split(":", 1)[1].strip()
    if not trigger_time:
        from datetime import datetime, timezone
        trigger_time = datetime.now(timezone.utc).isoformat()
    return trigger_time, trace_ids


# ═══════════════════════════════════════════════════════════════════════
# 诊断
# ═══════════════════════════════════════════════════════════════════════


async def diagnose_via_copilotkit(
    user_report: str,
    trigger_time: str | None = None,
    trace_ids: list[str] | None = None,
) -> dict[str, Any]:
    """用 CopilotKit 图路径诊断（bug_info → diagnosis_agent）。

    模拟用户聊天：把 user_report 作为 messages 的最后一条。
    同时把 trigger_time / trace_ids 注入到 state 中供 bug_info 使用。
    """
    from src.graph.copilotkit_graph import get_copilotkit_graph

    graph = get_copilotkit_graph()

    # 构造 CopilotKit 风格的 state
    state: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": user_report}
        ],
    }

    # 如果提供了 trigger_time/trace_ids，注入到 state（bug_info REST API path 会用到）
    if trigger_time or trace_ids:
        # 构造一个简易 raw_evidence 对象
        from src.graph.state import Evidence
        evidence = Evidence(
            user_report=user_report,
            trigger_time=trigger_time,
            trigger_trace_ids=trace_ids or [],
        )
        state["raw_evidence"] = evidence

    thread_id = f"cktest-{__import__('uuid').uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n  Thread: {thread_id}")
    print(f"  User message: {user_report[:120]}...")
    if trigger_time:
        print(f"  Trigger time: {trigger_time}")
    if trace_ids:
        print(f"  Trace IDs: {trace_ids}")

    print("  Running bug_info → diagnosis_agent ...")
    result = await graph.ainvoke(state, config)

    report = result.get("report")
    evidence = result.get("evidence")
    findings = result.get("findings", [])

    print(f"\n  ── Diagnosis ──")
    if report and hasattr(report, "model_dump"):
        r = report.model_dump()
        print(f"  Categories:    {r.get('categories', [])}")
        print(f"  Root cause:    {str(r.get('root_cause', ''))[:200]}")
        print(f"  Affected file: {r.get('affected_file', 'N/A')}")
        print(f"  Confidence:    {r.get('confidence', 0):.0%}")
        print(f"  Tier:          {r.get('root_cause_tier', 'N/A')}")
    else:
        print(f"  Report: {report}")

    if evidence and hasattr(evidence, "golden_signals"):
        print(f"  Signals:       {len(evidence.golden_signals)}")
    print(f"  Findings:      {len(findings)}")

    return {"report": report, "evidence": evidence, "findings": findings}


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test CopilotKit diagnosis path")
    parser.add_argument("recipe_id", nargs="?", help="Bug recipe ID (e.g. BE-020)")
    parser.add_argument("--skip-inject", action="store_true", help="Skip bug injection")
    parser.add_argument("--skip-trigger", action="store_true", help="Skip trigger (use --user-report directly)")
    parser.add_argument("--user-report", type=str, default="", help="User bug report text")
    parser.add_argument("--trigger-time", type=str, default="", help="Override trigger time (ISO 8601)")
    args = parser.parse_args()

    trigger_time = args.trigger_time or ""
    trace_ids: list[str] = []
    user_report = args.user_report

    if args.recipe_id and not args.skip_inject:
        print(f"[1/3] Injecting bug: {args.recipe_id} ...")
        inject_bug(args.recipe_id)
        print(f"  Waiting for uvicorn reload ({RELOAD_WAIT}s)...")
        time.sleep(RELOAD_WAIT)

    if args.recipe_id and not args.skip_trigger:
        print(f"[2/3] Triggering bug: {args.recipe_id} ...")
        trigger_time, trace_ids = trigger_bug(args.recipe_id)
        print(f"  Trigger time: {trigger_time}")
        print(f"  Trace IDs: {trace_ids}")
        print("  Waiting for Loki/Tempo indexing (3s)...")
        await asyncio.sleep(3)

    if not user_report and args.recipe_id:
        # 从 recipe 读取默认 user_report
        import yaml
        recipe_path = BUG_FACTORY_DIR / "recipes" / "gold" / f"{args.recipe_id}.yaml"
        if recipe_path.exists():
            recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
            user_report = recipe.get("input", {}).get("user_report", f"Bug {args.recipe_id}")

    if not user_report:
        user_report = input("Enter user bug report: ").strip()
        if not user_report:
            print("No user report provided. Exiting.")
            return

    print(f"\n[3/3] Diagnosing via CopilotKit path ...")
    result = await diagnose_via_copilotkit(
        user_report=user_report,
        trigger_time=trigger_time or None,
        trace_ids=trace_ids or None,
    )

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
