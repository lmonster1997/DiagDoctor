"""生成 CopilotKit 前端可用的 Bug 聊天消息。

注入 Bug → 触发 → 生成自然语言消息（含触发时间 + Trace ID）→ 复制到前端聊天框。

用法:
    # 全流程：注入 + 触发 + 生成消息
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020

    # 跳过注入（Bug 已在代码中）
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py BE-020 --skip-inject

    # 跳过所有，直接给消息文本
    & ".venv\Scripts\python.exe" scripts/test_copilotkit.py --msg "刚才给任务发评论报了500"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # DiagDoctor/
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"

PYTHON = sys.executable
DEMO_URL = "http://localhost:8000"
RELOAD_WAIT = 5


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
    trace_ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("TRACE_IDS_JSON="):
            payload = json.loads(line[len("TRACE_IDS_JSON="):])
            trace_ids = list(payload.get("trace_ids", []))
    from datetime import datetime, timezone
    trigger_time = datetime.now(timezone.utc).isoformat()
    return trigger_time, trace_ids


def generate_chat_message(
    user_report: str,
    trigger_time: str = "",
) -> str:
    """生成前端聊天消息：配方原始描述 + 触发时间。"""
    if trigger_time:
        return f"{user_report}\n\n（触发时间：{trigger_time}）"
    return user_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CopilotKit chat message for bug diagnosis")
    parser.add_argument("recipe_id", nargs="?", help="Bug recipe ID (e.g. BE-020)")
    parser.add_argument("--skip-inject", action="store_true", help="Skip bug injection")
    parser.add_argument("--skip-trigger", action="store_true", help="Skip trigger")
    parser.add_argument("--msg", type=str, default="", help="Direct message text (skip recipe lookup)")
    args = parser.parse_args()

    # ── 获取 user_report ──
    user_report = args.msg
    if not user_report and args.recipe_id:
        import yaml
        recipe_path = BUG_FACTORY_DIR / "output" / args.recipe_id / "case.yaml"
        if recipe_path.exists():
            recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
            user_report = recipe.get("input", {}).get("user_report", "")
    if not user_report:
        user_report = input("Enter bug description: ").strip()
        if not user_report:
            print("No input. Exiting.")
            return

    # ── 注入 ──
    if args.recipe_id and not args.skip_inject:
        print(f"[1/2] Injecting bug: {args.recipe_id} ...")
        inject_bug(args.recipe_id)
        print(f"  Waiting for uvicorn reload ({RELOAD_WAIT}s)...")
        time.sleep(RELOAD_WAIT)

    # ── 触发 ──
    trigger_time = ""
    trace_ids: list[str] = []
    if args.recipe_id and not args.skip_trigger:
        print(f"[2/2] Triggering bug: {args.recipe_id} ...")
        trigger_time, trace_ids = trigger_bug(args.recipe_id)
        print(f"  Trigger time: {trigger_time}")
        print(f"  Trace IDs:    {trace_ids}")
        await_loki = 3
        print(f"  Waiting for Loki/Tempo indexing ({await_loki}s)...")
        time.sleep(await_loki)

    # ── 生成消息：配方描述 + 触发时间 ──
    chat_message = generate_chat_message(user_report, trigger_time)

    print(f"\n{'=' * 60}")
    print(f"  📋  Copy this to the CopilotKit frontend chat:")
    print(f"{'=' * 60}")
    print(chat_message)
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()


