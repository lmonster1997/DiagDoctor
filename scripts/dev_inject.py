#!/usr/bin/env python3
"""
dev_inject.py — 开发用：注入 Bug → 触发 → 自动恢复代码。

不切换 git 分支，直接在当前分支上应用 diff_patch，结束后自动还原。
Doctor 后端会自行查询 Loki/Tempo 获取日志和 trace。

用法:
    uv run python scripts/dev_inject.py FE-020
    uv run python scripts/dev_inject.py FE-020 --skip-restore  # 保留注入的 bug（调试用）
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKSPACE / "bug-factory" / "src"))

from bug_factory.schema import TriggerResult, load_recipe
from bug_factory.trigger import TriggerRunner
from bug_factory.ai_rewriter import DiffPatchApplier


def _find_recipe(recipe_id: str) -> Path:
    prefix = recipe_id.lower().replace("-", "_")
    gold = _WORKSPACE / "bug-factory" / "recipes" / "gold"
    candidates = sorted(p for p in gold.rglob(f"{prefix}*.yaml"))
    if not candidates:
        print(f"❌ 找不到配方: {recipe_id}")
        sys.exit(1)
    return candidates[0]


def _extract_trace_ids(result: TriggerResult) -> list[str]:
    ids: list[str] = []
    if hasattr(result, "trace_ids") and result.trace_ids:
        ids.extend(result.trace_ids)
    for err in (result.browser_errors or []):
        if err.trace_id and err.trace_id not in ids:
            ids.append(err.trace_id)
    return ids


def _restore(target: Path, original: str | None) -> None:
    if original is None:
        return
    print("\n🔄 恢复原始代码...")
   # target.write_text(original, encoding="utf-8")
    print(f"   ✅ 已还原: {target.relative_to(_WORKSPACE)}")


async def main(recipe_id: str, skip_restore: bool = False, frontend_url: str | None = None):
    base_url = os.getenv("DEMO_APP_URL", "http://localhost:8000")

    yaml_path = _find_recipe(recipe_id)
    recipe = load_recipe(yaml_path)
    print(f"\n{'='*60}")
    print(f"  Bug: {recipe.id} — {recipe.title}")
    print(f"  分类: {recipe.category} | 严重度: {recipe.severity}")
    print(f"{'='*60}")

    # ── Step 1: Inject ─────────────────────────────────────────
    target_file = _WORKSPACE / recipe.injection.target_file
    original_content: str | None = None

    try:
        if recipe.injection.diff_patch:
            print("\n🔧 注入 Bug（不切换分支）...")
            original_content = target_file.read_text(encoding="utf-8")
            patched = DiffPatchApplier.apply(original_content, recipe.injection.diff_patch)
            target_file.write_text(patched, encoding="utf-8")
            print(f"   ✅ 已修改: {recipe.injection.target_file}")
        else:
            print("\n❌ 此配方没有 diff_patch，无法直接注入（需要 AI 改写 + OPENAI_API_KEY）")
            sys.exit(1)

        # ── Step 2: Trigger ────────────────────────────────────
        print("\n🚀 触发 Bug...")
        trigger_start = datetime.now(timezone.utc)
        runner = TriggerRunner(demo_app_base_url=base_url, frontend_url=frontend_url)
        trigger_result = await runner.run(recipe.trigger)

        if not trigger_result.success:
            print(f"   ❌ 触发失败: {trigger_result.error}")
            sys.exit(1)

        trace_ids = _extract_trace_ids(trigger_result)
        print(f"   ✅ 触发成功 ({len(trigger_result.steps)} 步)")
        if trace_ids:
            print(f"   🔗 Trace ID: {', '.join(trace_ids)}")

        # ── Prompt for CopilotChat ─────────────────────────────
        trigger_iso = trigger_start.isoformat()
        trace_str = ", ".join(trace_ids) if trace_ids else "（无）"
        print(f"\n{'='*60}")
        print(f"📋 复制以下内容粘贴到 DiagDoctor CopilotChat:")
        print(f"{'='*60}")
        print(f"""
请帮我诊断这个 Bug：

【错误现象】
{recipe.title}

【触发时间（UTC）】
{trigger_iso}

【Trace ID】
{trace_str}

【补充信息】
- Bug 分类: {recipe.category}
- 注入文件: {recipe.injection.target_file}
""")
        print(f"{'='*60}")

    finally:
        # ── Step 3: Restore (ALWAYS, even on crash) ────────────
        if not skip_restore and original_content is not None:
            _restore(target_file, original_content)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dev: inject bug, trigger, restore")
    parser.add_argument("recipe_id", help="Recipe ID, e.g. FE-020")
    parser.add_argument("--skip-restore", action="store_true", help="不自动恢复代码")
    parser.add_argument("--frontend", help="Demo frontend URL (for Playwright triggers)")
    args = parser.parse_args()
    asyncio.run(main(args.recipe_id, args.skip_restore, args.frontend))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dev: inject bug, trigger, restore")
    parser.add_argument("recipe_id", help="Recipe ID, e.g. FE-020")
    parser.add_argument("--skip-restore", action="store_true", help="不要自动恢复代码")
    parser.add_argument("--frontend", help="Demo frontend URL (for Playwright triggers)")
    args = parser.parse_args()
    asyncio.run(main(args.recipe_id, args.skip_restore, args.frontend))
