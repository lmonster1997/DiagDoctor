#!/usr/bin/env python3
"""
dev_inject.py — 开发用：一键注入 + 触发指定 Bug 配方，输出诊断关键信息。

用法:
    uv run python scripts/dev_inject.py FE-020
    uv run python scripts/dev_inject.py FE-020 --skip-inject  # 只触发（bug 已注入）
    uv run python scripts/dev_inject.py FE-020 --frontend http://localhost:5173

输出:
    - trigger_time: 粘贴到 CopilotChat 告诉 agent 事发时间
    - trace_id(s): 如果触发过程中有 API 调用，会捕获 traceparent
    - 一键复制到剪贴板的诊断提示语
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

# ── Add workspace to path ─────────────────────────────────────────
_WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKSPACE / "bug-factory" / "src"))

from bug_factory.schema import (
    CollectedEvidence,
    InjectionResult,
    TriggerResult,
    load_recipe,
)
from bug_factory.injector import BugInjector
from bug_factory.trigger import TriggerRunner
from bug_factory.evidence_collector import EvidenceCollector

# ── Helpers ────────────────────────────────────────────────────────

def _find_recipe(recipe_id: str) -> Path:
    prefix = recipe_id.lower().replace("-", "_")
    gold = _WORKSPACE / "bug-factory" / "recipes" / "gold"
    candidates = sorted(p for p in gold.rglob(f"{prefix}*.yaml"))
    if not candidates:
        print(f"❌ 找不到配方: {recipe_id} (搜索 {gold})")
        sys.exit(1)
    if len(candidates) > 1:
        print(f"⚠ 多个匹配: {[c.name for c in candidates]}")
    return candidates[0]


def _get_llm(recipe_has_diff: bool = False):
    from langchain_openai import ChatOpenAI
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        if recipe_has_diff:
            # Recipe has diff_patch → no LLM needed; return dummy
            print("   ℹ 无需 LLM（配方使用 diff_patch 直接注入）")
            return ChatOpenAI(model="gpt-4o", openai_api_key="sk-dummy")
        print("❌ 未设置 OPENAI_API_KEY 或 LLM_API_KEY（此配方需要 AI 改写）")
        sys.exit(1)
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", "gpt-4o"),
        openai_api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL") or None,
        temperature=0.2,
    )


def _extract_trace_ids(trigger_result: TriggerResult) -> list[str]:
    """Extract W3C trace_ids from trigger steps (from traceparent headers)."""
    ids: list[str] = []
    for step in trigger_result.steps:
        tid = step.session.get("trace_id") or step.session.get("traceparent", "")
        if tid and len(tid) == 32:  # W3C trace_id is 32 hex chars
            ids.append(tid)
    # Also check browser errors
    for err in (trigger_result.browser_errors or []):
        if err.trace_id and err.trace_id not in ids:
            ids.append(err.trace_id)
    return ids


# ── Main ───────────────────────────────────────────────────────────

async def main(recipe_id: str, skip_inject: bool = False, frontend_url: str | None = None):
    # Config
    base_url = os.getenv("DEMO_APP_URL", "http://localhost:8000")
    loki_url = os.getenv("LOKI_URL", "http://localhost:3100")
    tempo_url = os.getenv("TEMPO_URL", "http://localhost:3200")

    # Load recipe
    yaml_path = _find_recipe(recipe_id)
    recipe = load_recipe(yaml_path)
    print(f"\n{'='*60}")
    print(f"  Bug: {recipe.id} — {recipe.title}")
    print(f"  分类: {recipe.category} | 严重度: {recipe.severity}")
    print(f"{'='*60}")

    # Step 1: Inject
    if not skip_inject:
        print("\n🔧 Step 1/3: 注入 Bug...")
        has_diff = bool(recipe.injection and recipe.injection.diff_patch)
        llm = _get_llm(recipe_has_diff=has_diff)
        injector = BugInjector(repo_path=_WORKSPACE, llm=llm)
        result = await injector.inject(recipe)
        print(f"   ✅ 分支: {result.branch}")
        print(f"   📝 修改文件: {result.modified_files}")
        print(f"\n   ⚠ 请重启 demo-app 让注入的 bug 生效！")
    else:
        print("\n⏭ Step 1/3: 跳过注入（--skip-inject）")

    # Step 2: Trigger
    print("\n🚀 Step 2/3: 触发 Bug...")
    trigger_start = datetime.now(timezone.utc)
    runner = TriggerRunner(demo_app_base_url=base_url, frontend_url=frontend_url)
    trigger_result = await runner.run(recipe.trigger)

    if not trigger_result.success:
        print(f"   ❌ 触发失败: {trigger_result.error}")
        sys.exit(1)

    trace_ids = _extract_trace_ids(trigger_result)
    print(f"   ✅ 触发成功 ({len(trigger_result.steps)} 步)")
    if trace_ids:
        print(f"   🔗 Trace IDs: {', '.join(trace_ids)}")

    # Step 3: Collect evidence (brief)
    print("\n📊 Step 3/3: 等待日志落盘 + 采集证据...")
    print("   ⏳ 等待 15s (OTel pipeline flush)...")
    await asyncio.sleep(15)

    collector = EvidenceCollector(loki_url=loki_url, tempo_url=tempo_url)
    evidence = await collector.collect(
        recipe_id=recipe.id,
        start=trigger_start - timedelta(seconds=30),
        end=datetime.now(timezone.utc),
        browser_errors=trigger_result.browser_errors or [],
    )

    log_count = len(evidence.logs)
    trace_count = len(evidence.traces)
    print(f"   📋 日志: {log_count} 条 | Trace spans: {trace_count} 个")

    # ── Output: what to paste into CopilotChat ──
    trigger_iso = trigger_start.isoformat()
    trace_str = ", ".join(trace_ids) if trace_ids else "（无 — 纯前端错误可能没有 trace）"

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
- 注入文件: {recipe.injection.target_file if recipe.injection else 'N/A'}
- 已采集 {log_count} 条日志, {trace_count} 个 trace span
""")
    print(f"{'='*60}")

    # Save evidence for reference
    out_dir = _WORKSPACE / "bug-factory" / "output" / recipe.id
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = out_dir / "dev_evidence.json"
    evidence_path.write_text(
        evidence.model_dump_json(indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n💾 证据已保存: {evidence_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dev: inject + trigger a bug recipe")
    parser.add_argument("recipe_id", help="Recipe ID, e.g. FE-020")
    parser.add_argument("--skip-inject", action="store_true", help="Skip injection, only trigger")
    parser.add_argument("--frontend", help="Demo frontend URL (for Playwright triggers)")
    args = parser.parse_args()
    asyncio.run(main(args.recipe_id, args.skip_inject, args.frontend))
