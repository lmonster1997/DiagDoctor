"""Test CopilotKit 诊断路径：注入 Bug → 触发 → 生成聊天文本 → 诊断。

用法:
    # 全流程
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020

    # 只诊断（跳过注入+触发）
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py --skip-inject --msg "刚才给任务发评论报了500"

    # 只生成消息文本，不诊断
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020 --gen-only
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
    if not trigger_time:
        from datetime import datetime, timezone
        trigger_time = datetime.now(timezone.utc).isoformat()
    return trigger_time, trace_ids


# ═══════════════════════════════════════════════════════════════════════
# 聊天消息生成（模拟前端用户输入）
# ═══════════════════════════════════════════════════════════════════════


def generate_chat_message(
    user_report: str,
    trigger_time: str = "",
    trace_ids: list[str] | None = None,
) -> str:
    """生成模拟前端聊天输入的自然语言消息。

    bug_info 节点会从这个消息中 LLM 提取 trigger_time、trace_ids 等结构化信息。
    因此需要把这些信息以自然语言形式嵌入消息文本中。
    """
    parts = [user_report]

    if trigger_time:
        # 用中文自然表达时间，让 LLM 能解析
        parts.append(f"触发时间大约是 {trigger_time}。")

    if trace_ids:
        ids_str = "、".join(trace_ids[:3])
        parts.append(f"相关的 trace id 有：{ids_str}。")

    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# 诊断（纯 CopilotKit 路径：messages → LLM 提取 → auto-prefetch → 诊断）
# ═══════════════════════════════════════════════════════════════════════


async def diagnose_via_copilotkit(
    chat_message: str,
) -> dict[str, Any]:
    """用 CopilotKit 图路径诊断。

    传入纯自然语言聊天消息，走完整的 CopilotKit 路径：
    bug_info (LLM 提取结构化信息 → auto-prefetch → normalize)
    → diagnosis_agent (ReAct 诊断)。

    不传 raw_evidence——让 bug_info 自己从消息中提取 trigger_time/trace_ids。
    """
    from src.graph.copilotkit_graph import get_copilotkit_graph

    graph = get_copilotkit_graph()

    # 纯 CopilotKit 风格：只用 messages，不传 structured evidence
    state: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": chat_message}
        ],
    }

    thread_id = f"cktest-{__import__('uuid').uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n  Thread: {thread_id}")
    print(f"  Chat message ({len(chat_message)} chars):")
    print(f"  ┌{'─' * 60}")
    for line in chat_message.split("\n"):
        print(f"  │ {line[:70]}")
    print(f"  └{'─' * 60}")

    print("\n  Running bug_info → diagnosis_agent (CopilotKit path) ...")
    result = await graph.ainvoke(state, config)

    # ── 输出 ──
    report = result.get("report")
    evidence = result.get("evidence")
    findings = result.get("findings", [])
    bug_info_meta = result.get("bug_info", {})

    print(f"\n  {'─' * 50}")
    print(f"  Bug Info (LLM 提取):")
    print(f"    trigger_time: {bug_info_meta.get('trigger_time', 'N/A')}")
    print(f"    trace_ids:    {bug_info_meta.get('trace_ids', [])}")
    print(f"    description:  {str(bug_info_meta.get('bug_description', ''))[:80]}")

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

    return {"report": report, "evidence": evidence, "findings": findings, "bug_info": bug_info_meta}


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test CopilotKit diagnosis path (free-text chat)")
    parser.add_argument("recipe_id", nargs="?", help="Bug recipe ID (e.g. BE-020)")
    parser.add_argument("--skip-inject", action="store_true", help="Skip bug injection")
    parser.add_argument("--skip-trigger", action="store_true", help="Skip trigger")
    parser.add_argument("--msg", type=str, default="", help="Direct chat message (skip recipe lookup)")
    parser.add_argument("--gen-only", action="store_true", help="Only generate chat message, don't diagnose")
    args = parser.parse_args()

    # ── 获取 user_report ──
    user_report = args.msg
    if not user_report and args.recipe_id:
        import yaml
        recipe_path = BUG_FACTORY_DIR / "recipes" / "gold" / f"{args.recipe_id}.yaml"
        if recipe_path.exists():
            recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
            user_report = recipe.get("input", {}).get("user_report", "")
    if not user_report:
        user_report = input("Enter user bug report: ").strip()
        if not user_report:
            print("No input. Exiting.")
            return

    # ── 注入 + 触发 ──
    trigger_time = ""
    trace_ids: list[str] = []

    if args.recipe_id and not args.skip_inject:
        print(f"[1/3] Injecting bug: {args.recipe_id} ...")
        inject_bug(args.recipe_id)
        print(f"  Waiting for uvicorn reload ({RELOAD_WAIT}s)...")
        time.sleep(RELOAD_WAIT)

    if args.recipe_id and not args.skip_trigger:
        print(f"[2/3] Triggering bug: {args.recipe_id} ...")
        trigger_time, trace_ids = trigger_bug(args.recipe_id)
        print(f"  Trigger time: {trigger_time}")
        print(f"  Trace IDs:    {trace_ids}")
        print("  Waiting for Loki/Tempo indexing (3s)...")
        await asyncio.sleep(3)

    # ── 生成聊天消息 ──
    chat_message = generate_chat_message(user_report, trigger_time, trace_ids)

    if args.gen_only:
        print(f"\n  ┌─ Generated Chat Message (copy to frontend) {'─' * 20}")
        print(f"  │ {chat_message}")
        print(f"  └{'─' * 60}")
        return

    # ── CopilotKit 诊断 ──
    print(f"\n[3/3] Diagnosing via CopilotKit path (free-text) ...")
    await diagnose_via_copilotkit(chat_message)

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())

