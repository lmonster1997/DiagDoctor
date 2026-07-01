"""Langfuse 基线 Experiment：注入 Bug → 触发 → 诊断 → 打分 → 恢复。

与 bug-factory 的分工：
  - bug-factory：inject（改代码）+ trigger（发请求）——只"布置考场"
  - Doctor：search_observability（实时查 Loki/Tempo）——自己"收集证据"
  - Experiment：串联上述流程 + 打分 + 恢复现场

运行前确保：
  - demo-app backend 运行在 http://localhost:8000（uvicorn --reload）
  - Doctor API 运行在 http://localhost:8001
  - Loki/Tempo 可访问
  - 当前在 git main 分支且工作区干净

用法：
    cd doctor && uv run python scripts/run_baseline_experiment.py
    cd doctor && uv run python scripts/run_baseline_experiment.py --limit 3  # 只跑前3个
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from langfuse import Langfuse

# ── 路径常量 ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUG_FACTORY_DIR = PROJECT_ROOT.parent / "bug-factory"

# 添加 doctor 到 path 以便 import settings
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import settings  # noqa: E402

# ── 可配置参数 ─────────────────────────────────────────────────────────
DEMO_BACKEND_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"
RELOAD_WAIT = 5  # uvicorn reload 等待秒数
DIAGNOSE_TIMEOUT = 12000  # 单次诊断超时秒数
LOKI_INDEX_DELAY = 3  # Loki/Tempo 索引延迟


# ═══════════════════════════════════════════════════════════════════════
# Langfuse 客户端
# ═══════════════════════════════════════════════════════════════════════

langfuse = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════


def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    """运行命令，失败时抛出异常。"""
    print(f"  > {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **__import__("os").environ,
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",  # 抑制 Rich 颜色输出（避免 Windows GBK 问题）
            "TERM": "dumb",  # 禁用 Rich 终端特性
            "FORCE_COLOR": "0",
        },
    )
    if result.returncode != 0:
        stderr = result.stderr[-500:] if result.stderr else ""
        raise RuntimeError(f"命令失败 (exit={result.returncode}): {stderr}")
    if result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        for line in lines[-5:]:
            print(f"    {line}")


def git_checkout_main() -> None:
    """切换到 main 分支，确保干净起点。"""
    run_cmd(["git", "checkout", "main"], cwd=PROJECT_ROOT.parent)
    print("  ✓ 已切换到 main 分支")


def inject_bug(recipe_id: str) -> None:
    """注入 Bug：修改源码。"""
    run_cmd(
        ["uv", "run", "python", "-m", "bug_factory.cli", "inject", recipe_id],
        cwd=BUG_FACTORY_DIR,
    )


def trigger_bug(recipe_id: str) -> datetime:
    """触发 Bug：对 demo-app 发起请求，产生日志和 Trace。

    返回触发开始时间（UTC），供 Doctor 缩小 Loki/Tempo 查询窗口。
    不加 --no-ui，保持真实用户操作路径。
    """
    trigger_start = datetime.now(UTC)
    run_cmd(
        [
            "uv",
            "run",
            "python",
            "-m",
            "bug_factory.cli",
            "trigger",
            recipe_id,
            "--base-url",
            DEMO_BACKEND_URL,
        ],
        cwd=BUG_FACTORY_DIR,
    )
    return trigger_start


async def wait_for_backend(url: str, max_wait: int = 30) -> bool:
    """等待后端就绪（Bug 注入后 uvicorn reload 需要时间）。"""
    async with aiohttp.ClientSession() as session:
        for _ in range(max_wait):
            try:
                async with session.get(
                    f"{url}/health",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)
    return False


async def call_doctor(user_report: str, trigger_time: datetime) -> dict:
    """调用 Doctor API 执行诊断。

    传入 trigger_time，Doctor 的 search_observability 工具用它缩小
    Loki/Tempo 查询窗口（trigger_time ± 5min）。
    """
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{DOCTOR_URL}/api/diagnose",
            json={
                "evidence": {"user_report": user_report},
                "trigger_time": trigger_time.isoformat(),
            },
            timeout=aiohttp.ClientTimeout(total=DIAGNOSE_TIMEOUT),
        ) as resp,
    ):
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Doctor API 返回 {resp.status}: {text[:500]}")
        return await resp.json()


# ═══════════════════════════════════════════════════════════════════════
# Experiment task（每个 case 执行一次）
# ═══════════════════════════════════════════════════════════════════════


async def diagnose_task(item, trace_id: str) -> dict:
    """完整的"布置考场 → 诊断 → 清理"流水线。"""
    recipe_id = item.metadata.get("recipe_id", "unknown")
    user_report = item.input.get("user_report", "")

    print(f"\n{'=' * 60}")
    print(f"  Case: {recipe_id}")
    print(f"  User Report: {user_report[:80]}...")
    print(f"{'=' * 60}")

    # ── Step 1: 恢复干净起点 ───────────────────────────────────────
    print("[1/4] 恢复 git main 分支...")
    git_checkout_main()

    # ── Step 2: 注入 Bug ──────────────────────────────────────────
    print(f"[2/4] 注入 Bug: {recipe_id}...")
    inject_bug(recipe_id)
    print(f"  等待 uvicorn reload ({RELOAD_WAIT}s)...")
    time.sleep(RELOAD_WAIT)

    if not await wait_for_backend(DEMO_BACKEND_URL):
        raise RuntimeError(f"Demo backend 未在 {RELOAD_WAIT + 30}s 内就绪")
    print("  ✓ Demo backend 已就绪")

    # ── Step 3: 触发 Bug + 记录时间 ───────────────────────────────
    print(f"[3/4] 触发 Bug: {recipe_id}...")
    trigger_time = trigger_bug(recipe_id)
    print(f"  触发时间: {trigger_time.isoformat()}")
    print(f"  等待 Loki/Tempo 索引 ({LOKI_INDEX_DELAY}s)...")
    await asyncio.sleep(LOKI_INDEX_DELAY)

    # ── Step 4: 调用 Doctor（证据由 Doctor 自己实时查询） ─────────
    print("[4/4] 调用 Doctor API 诊断...")
    try:
        diagnosis = await call_doctor(user_report, trigger_time)
    except Exception as exc:
        print(f"  ✗ 诊断失败: {exc}")
        diagnosis = {"error": str(exc), "report": None, "categories": [], "confidence": 0.0}

    report = diagnosis.get("report") or {}
    categories = (
        report.get("categories", [])
        if isinstance(report, dict)
        else diagnosis.get("categories", [])
    )
    confidence = (
        report.get("confidence", 0) if isinstance(report, dict) else diagnosis.get("confidence", 0)
    )
    print(f"  ✓ 诊断完成（categories={categories}, confidence={confidence}）")

    # ── 恢复现场 ──────────────────────────────────────────────────
    print("  恢复 git main 分支...")
    git_checkout_main()
    time.sleep(2)

    return diagnosis


# ═══════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════


async def main(items: list | None = None) -> None:
    print("=" * 60)
    print("  DiagDoctor 基线 Experiment (Phase 0)")
    print("=" * 60)

    # 前置检查
    print("\n── 前置检查 ──")
    async with aiohttp.ClientSession() as session:
        for name, url in [("Doctor API", DOCTOR_URL), ("Demo Backend", DEMO_BACKEND_URL)]:
            try:
                async with session.get(
                    f"{url}/health", timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    assert resp.status == 200
                    print(f"  ✓ {name}: {url}")
            except Exception:
                print(f"  ✗ {name} 不可达: {url}")
                sys.exit(1)

    # 确保在 main 分支
    git_checkout_main()

    # 获取 Dataset
    dataset = langfuse.get_dataset("diagdoctor-benchmark")
    if items is None:
        items = sorted(dataset.items, key=lambda it: it.metadata.get("bug_id", "Z99"))

    print(f"\n  Dataset: diagdoctor-benchmark ({len(items)} items)")

    # 逐个运行
    print("\n── 开始逐个运行 case ──")
    results: list[dict] = []
    for i, item in enumerate(items):
        metadata = item.metadata or {}
        recipe_id = metadata.get("recipe_id", "unknown")

        print(f"\n{'─' * 60}")
        print(f"  [{i + 1}/{len(items)}] {recipe_id}")
        print(f"{'─' * 60}")

        # 创建 Langfuse trace
        trace = langfuse.trace(
            name=f"baseline_phase0_{recipe_id}",
            metadata={"recipe_id": recipe_id, "run": "baseline_phase0"},
        )

        try:
            result = await diagnose_task(item, trace_id=trace.id)

            # ── Scorer ──
            expected_output = item.expected_output or {}

            # Extract prediction
            report = result.get("report") or {}
            pred_categories = set(
                report.get("categories", [])
                if isinstance(report, dict)
                else result.get("categories", [])
            )
            gold_categories = set(expected_output.get("category", []))
            pred_primary = (
                report.get("primary_category", "")
                if isinstance(report, dict)
                else result.get("primary_category", "")
            )
            # Fallback: if primary_category not set, use first category
            if not pred_primary and pred_categories:
                pred_primary = next(iter(sorted(pred_categories)), "")
            gold_primary = expected_output.get("primary_category", "")
            if not gold_primary and gold_categories:
                gold_primary = next(iter(sorted(gold_categories)), "")

            # 1. category_accuracy: binary primary_category match
            category_hit = (
                1.0 if pred_primary and gold_primary and pred_primary == gold_primary else 0.0
            )
            trace.score(name="category_accuracy", value=category_hit)
            print(
                f"    category_accuracy: {category_hit:.2f} "
                f"(pred={pred_primary}, gold={gold_primary}, "
                f"pred_set={sorted(pred_categories)}, gold_set={sorted(gold_categories)})"
            )

            # 1b. categories_f1: multi-label F1 (secondary metric)
            if gold_categories:
                tp = len(pred_categories & gold_categories)
                precision = tp / len(pred_categories) if pred_categories else 0.0
                recall = tp / len(gold_categories)
                f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            else:
                f1 = 1.0 if not pred_categories else 0.0
            trace.score(name="categories_f1", value=f1)
            print(f"    categories_f1: {f1:.2f} (P={precision:.2f} R={recall:.2f})")

            # 2. affected_file_accuracy
            expected_file = expected_output.get("affected_file", "")
            actual_file = (
                report.get("affected_file", "")
                if isinstance(report, dict)
                else result.get("affected_file", "")
            )

            # Normalize path comparison: LLM may output backend-relative path
            # (e.g. "app/api/comments.py") while gold recipe uses repo-root
            # relative path (e.g. "demo-app/backend/app/api/comments.py").
            # Strategy: check if either path's suffix matches the other.
            file_hit = 0.0
            if actual_file and expected_file:
                actual_path = Path(str(actual_file))
                expected_path = Path(str(expected_file))
                actual_parts = actual_path.parts
                expected_parts = expected_path.parts
                # Check if one path ends with the other's tail parts
                if (
                    actual_parts[-len(expected_parts) :] == expected_parts
                    if len(actual_parts) >= len(expected_parts)
                    else expected_parts[-len(actual_parts) :] == actual_parts
                ):
                    file_hit = 1.0
            trace.score(name="affected_file_accuracy", value=file_hit)
            print(
                f"    affected_file_accuracy: {file_hit:.2f} "
                f"(expected={expected_file}, actual={actual_file})"
            )

            # 3. efficiency（占位，Phase 2 完善）
            trace.score(name="efficiency", value=0.5)

            results.append({"recipe_id": recipe_id, "success": True, **result})

        except Exception as exc:
            print(f"  ✗ Case 失败: {exc}")
            trace.score(name="category_accuracy", value=0.0)
            trace.score(name="affected_file_accuracy", value=0.0)
            results.append({"recipe_id": recipe_id, "success": False, "error": str(exc)})

        # 确保恢复 main 分支
        with contextlib.suppress(Exception):
            git_checkout_main()

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    success_count = sum(1 for r in results if r.get("success"))
    print(f"  完成: {success_count}/{len(results)} case 成功")
    print("  查看结果: Langfuse Dashboard → Traces")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DiagDoctor 基线 Experiment")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个 case")
    parser.add_argument(
        "--cases", type=str, default=None, help="逗号分隔的 recipe_id 列表，如 BE-020,FE-020"
    )
    args = parser.parse_args()

    # 获取并筛选 dataset items
    dataset = langfuse.get_dataset("diagdoctor-benchmark")
    items = sorted(dataset.items, key=lambda it: it.metadata.get("bug_id", "Z99"))
    if args.cases:
        case_set = {c.strip() for c in args.cases.split(",")}
        items = [it for it in items if it.metadata.get("bug_id", "") in case_set]
        print(f"筛选 case: {args.cases} → {[it.metadata.get('bug_id') for it in items]}")
    if args.limit:
        items = items[: args.limit]

    asyncio.run(main(items=items))
