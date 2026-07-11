"""后端 Agent 评测脚本：注入 Bug → 触发 → 诊断 → 打分 → 恢复。

自动化测试 Doctor 后端 agent 的诊断是否正常：对每个 case 注入 bug、
触发、调用 Doctor API 诊断、用 Langfuse 7 维度 Scorer + 过程质量打分，
最后还原代码。专门用于跑批量基线评测。

与 bug-factory 的分工：
  - bug-factory：inject（改代码）+ trigger（发请求）——只"布置考场"
  - Doctor：search_observability（实时查 Loki/Tempo）——自己"收集证据"
  - 本脚本：串联上述流程 + 打分 + 恢复现场

运行前确保：
  - demo-app backend 运行在 http://localhost:8000（uvicorn --reload）
  - Doctor API 运行在 http://localhost:8001
  - Loki/Tempo 可访问
  - 工作区干净（不切换 git 分支，直接在当前分支改文件，结束后还原）

用法：
    uv run python scripts/eval_agent.py
    uv run python scripts/eval_agent.py --limit 3  # 只跑前3个
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from langfuse import Langfuse

# ── 路径常量 ──────────────────────────────────────────────────────────
_WORKSPACE = Path(__file__).resolve().parent.parent
DOCTOR_BACKEND = _WORKSPACE / "doctor" / "backend"
BUG_FACTORY_DIR = _WORKSPACE / "bug-factory"
RECIPES_DIR = BUG_FACTORY_DIR / "recipes" / "gold"

# 添加 doctor/backend 到 path 以便 import settings 和 scripts.langfuse_scorers
sys.path.insert(0, str(DOCTOR_BACKEND))
# 添加 bug-factory 以便 import DiffPatchApplier / TriggerRunner
sys.path.insert(0, str(BUG_FACTORY_DIR / "src"))

# 显式加载 doctor/backend/.env（脚本从 workspace 根运行时，
# pydantic-settings 默认只从 CWD 找 .env，会漏掉 doctor/backend/.env）
from dotenv import load_dotenv  # noqa: E402

load_dotenv(DOCTOR_BACKEND / ".env")

# Langfuse 多维度 Scorer（D13 任务 2.1）
from scripts.langfuse_scorers import (  # noqa: E402
    score_all_dimensions,
    score_process_quality,
)
from src.config import settings  # noqa: E402

from bug_factory.schema import load_recipe  # noqa: E402
from bug_factory.ai_rewriter import DiffPatchApplier  # noqa: E402
from bug_factory.trigger import TriggerRunner  # noqa: E402

# ── 可配置参数 ─────────────────────────────────────────────────────────
DEMO_BACKEND_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"
RELOAD_WAIT = 20  # uvicorn reload 等待秒数（Windows 上需要 15-20s）
DIAGNOSE_TIMEOUT = 12000  # 单次诊断超时秒数
LOKI_INDEX_DELAY = 3  # Loki/Tempo 索引延迟

# ── 评测子集 ──────────────────────────────────────────────────
SMOKE_CASES: set[str] = {"BE-020", "FE-020", "PERF-020", "LOGIC-020"}

# ── Recipe cache ───────────────────────────────────────────────
_recipe_cache: dict[str, object] = {}

def _load_recipe(recipe_id: str):
    """Load recipe YAML by ID, cached."""
    if recipe_id not in _recipe_cache:
        prefix = recipe_id.lower().replace("-", "_")
        candidates = sorted(p for p in RECIPES_DIR.rglob(f"{prefix}*.yaml"))
        if not candidates:
            raise FileNotFoundError(f"找不到配方: {recipe_id}")
        _recipe_cache[recipe_id] = load_recipe(candidates[0])
    return _recipe_cache[recipe_id]


def inject_bug(recipe_id: str) -> tuple[Path, str]:
    """直接在当前分支应用 diff_patch，不切换 git 分支。
    返回 (目标文件路径, 原始内容) 供后续恢复。"""
    recipe = _load_recipe(recipe_id)
    target = _WORKSPACE / recipe.injection.target_file
    original = target.read_text(encoding="utf-8")
    if recipe.injection.diff_patch:
        patched = DiffPatchApplier.apply(original, recipe.injection.diff_patch)
        target.write_text(patched, encoding="utf-8")
        print(f"  [OK] 已注入: {recipe.injection.target_file}")
    return target, original


def restore_file(target: Path, original: str) -> None:
    """还原单个文件。"""
    target.write_text(original, encoding="utf-8")
    print(f"  [OK] 已还原: {target.relative_to(_WORKSPACE)}")


def _safe_restore(target: Path | None, original: str) -> None:
    """容错还原：忽略 None target 和异常。"""
    if target is None or not original:
        return
    with contextlib.suppress(Exception):
        restore_file(target, original)


async def trigger_bug_async(recipe_id: str) -> tuple[datetime, list[str]]:
    """用 TriggerRunner 直接触发 Bug，不经过 CLI 子进程。
    返回 (触发开始时间 UTC, trace_id 列表)。"""
    recipe = _load_recipe(recipe_id)
    trigger_start = datetime.now(UTC)
    runner = TriggerRunner(demo_app_base_url=DEMO_BACKEND_URL)
    result = await runner.run(recipe.trigger)
    if not result.success:
        raise RuntimeError(f"触发失败: {result.error}")
    trace_ids: list[str] = []
    if hasattr(result, "trace_ids") and result.trace_ids:
        trace_ids = list(result.trace_ids)
    for err in (result.browser_errors or []):
        if err.trace_id and err.trace_id not in trace_ids:
            trace_ids.append(err.trace_id)
    return trigger_start, trace_ids


# ═══════════════════════════════════════════════════════════════════════
# Langfuse 客户端
# ═══════════════════════════════════════════════════════════════════════

langfuse = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)


async def _health_ok_async(url: str, session: aiohttp.ClientSession) -> bool:
    """单次探测 /health，返回 True 如果 200。"""
    try:
        async with session.get(
            f"{url}/health", timeout=aiohttp.ClientTimeout(total=2)
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def wait_for_reload(url: str) -> bool:
    """等待 uvicorn --reload 完成（固定等待 20s + 健康检查）。

    在 Windows 上 reload 需要约 15-20 秒，且 /health 在整个过程中始终
    可用（旧 server 要等新 server 就绪后才关闭），无法用断崖检测。
    """
    await asyncio.sleep(RELOAD_WAIT)
    async with aiohttp.ClientSession() as session:
        if await _health_ok_async(url, session):
            return True
    # 再等 10s 重试
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        return await _health_ok_async(url, session)


async def call_doctor(
    user_report: str,
    trigger_time: datetime,
    trace_ids: list[str] | None = None,
    langfuse_trace_id: str | None = None,
) -> dict:
    """调用 Doctor API 执行诊断。

    传入 trigger_time，Doctor 的 search_observability 工具用它缩小
    Loki/Tempo 查询窗口（trigger_time ± 5min）。

    传入 trace_ids 时，Doctor ingest 优先按 trace_id 精准查 Tempo/Loki
    （取代宽时间窗），实现 batch 运行里每个 case 只看自己触发产生的信号，
    避免跨 case 日志污染。

    传入 langfuse_trace_id 时，Doctor agent 会把 LLM/tool observation
    记录到该 trace 上，使过程质量评分（score_process_quality）能读到
    完整调用过程。
    """
    payload: dict = {
        "evidence": {"user_report": user_report},
        "trigger_time": trigger_time.isoformat(),
    }
    if trace_ids:
        payload["trigger_trace_ids"] = trace_ids
    if langfuse_trace_id:
        payload["langfuse_trace_id"] = langfuse_trace_id

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{DOCTOR_URL}/api/diagnose",
            json=payload,
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
    """完整的"布置考场 → 诊断 → 清理"流水线（不切换 git 分支）。"""
    recipe_id = item.metadata.get("recipe_id", "unknown")
    user_report = item.input.get("user_report", "")

    print(f"\n{'=' * 60}")
    print(f"  Case: {recipe_id}")
    print(f"  User Report: {user_report[:80]}...")
    print(f"{'=' * 60}")

    target: Path | None = None
    original: str = ""

    try:
        # ── Step 1: 注入 Bug（直接改文件）───────────────────────────
        print(f"[1/4] 注入 Bug: {recipe_id}...")
        target, original = inject_bug(recipe_id)
        print(f"  等待 uvicorn reload...")
        if not await wait_for_reload(DEMO_BACKEND_URL):
            raise RuntimeError(f"Demo backend 未在 60s 内完成 reload")
        print("  [OK] Demo backend 已就绪（新代码已加载）")

        # ── Step 2: 触发 Bug + 记录时间 ─────────────────────────────
        print(f"[2/4] 触发 Bug: {recipe_id}...")
        trigger_time, trace_ids = await trigger_bug_async(recipe_id)
        print(f"  触发时间: {trigger_time.isoformat()}")
        print(f"  关联 trace_ids: {trace_ids or '(无)'}")
        print(f"  等待 Loki/Tempo 索引 ({LOKI_INDEX_DELAY}s)...")
        await asyncio.sleep(LOKI_INDEX_DELAY)

        # ── Step 3: 调用 Doctor（证据由 Doctor 自己实时查询） ───────
        print("[3/4] 调用 Doctor API 诊断...")
        try:
            diagnosis = await call_doctor(
                user_report,
                trigger_time,
                trace_ids=trace_ids,
                langfuse_trace_id=trace_id,
            )
        except Exception as exc:
            print(f"  [FAIL] 诊断失败: {exc}")
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
        print(f"  [OK] 诊断完成（categories={categories}, confidence={confidence}）")
    finally:
        # ── Step 4: 还原代码（无论成功或异常都必须执行）────────────
        print("[4/4] 还原代码...")
        _safe_restore(target, original)

    return diagnosis


# ═══════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════


async def main(
    items: list | None = None,
    run_name: str = "run",
    split: str = "all",
) -> None:
    print("=" * 60)
    print(f"  DiagDoctor 基线 Experiment (run={run_name}, split={split})")
    print(f"  Langfuse: Sessions → {run_name} 可看本轮全部 trace")
    print("=" * 60)

    # 前置检查
    print("\n-- 前置检查 --")
    async with aiohttp.ClientSession() as session:
        for name, url in [("Doctor API", DOCTOR_URL), ("Demo Backend", DEMO_BACKEND_URL)]:
            try:
                async with session.get(
                    f"{url}/health", timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    assert resp.status == 200
                    print(f"  [OK] {name}: {url}")
            except Exception:
                print(f"  [FAIL] {name} 不可达: {url}")
                sys.exit(1)

    # 获取 Dataset
    dataset = langfuse.get_dataset("diagdoctor-benchmark")
    if items is None:
        items = sorted(dataset.items, key=lambda it: it.metadata.get("bug_id", "Z99"))

    print(f"\n  Dataset: diagdoctor-benchmark ({len(items)} items)")

    # 逐个运行
    print("\n-- 开始逐个运行 case --")
    results: list[dict] = []
    for i, item in enumerate(items):
        metadata = item.metadata or {}
        recipe_id = metadata.get("recipe_id", "unknown")

        print(f"\n{'-' * 60}")
        print(f"  [{i + 1}/{len(items)}] {recipe_id}")
        print(f"{'-' * 60}")

        # 创建 Langfuse trace
        # session_id=run_name 把同一轮的多个 case 归到同一个 Session 视图，
        # 便于在 Langfuse Sessions 标签页一键找到本轮全部 trace（避免散落难找）。
        trace = langfuse.trace(
            name=f"{run_name}_{recipe_id}",
            session_id=run_name,
            tags=[split, "phase0"],
            metadata={
                "recipe_id": recipe_id,
                "run": run_name,
                "run_name": run_name,
                "split": split,
            },
        )

        try:
            result = await diagnose_task(item, trace_id=trace.id)

            # ── 7 维度 Scorer（D13 任务 2.1）──────────────────
            expected_output = item.expected_output or {}

            # Build unified diagnosis dict merging top-level + report fields
            report = result.get("report") or {}
            diagnosis_for_scorer: dict = {
                **result,
                **(report if isinstance(report, dict) else {}),
            }

            scores = await score_all_dimensions(
                langfuse,
                trace.id,
                expected_output,
                diagnosis_for_scorer,
                skip_llm_judge=False,
            )

            # ── 过程质量 Scorer（D14 任务 2.2）──────────────────
            # Doctor agent 已把 LLM/tool observation 记录到同一个 trace，
            # 这里读取 trace 的 observation 评估调用过程质量。
            # 短暂等待确保 Langfuse 服务端完成索引（flush 已在 agent 端完成）。
            await asyncio.sleep(1)
            process_score = score_process_quality(langfuse, trace.id)

            print(
                f"    overall: {scores.get('overall', 0):.2f} "
                f"(root_cause={scores.get('root_cause_accuracy', 0):.2f}, "
                f"category={scores.get('category_accuracy', 0):.2f}, "
                f"file={scores.get('affected_file_accuracy', 0):.2f}, "
                f"fix={scores.get('fix_suggestion_quality', 0):.2f}) "
                f"process_quality={process_score:.2f}"
            )

            results.append(
                {
                    "recipe_id": recipe_id,
                    "success": True,
                    "process_quality": process_score,
                    **result,
                }
            )

        except Exception as exc:
            print(f"  [FAIL] Case 失败: {exc}")
            trace.score(name="category_accuracy", value=0.0)
            trace.score(name="affected_file_accuracy", value=0.0)
            trace.score(name="overall", value=0.0)
            trace.score(name="process_quality", value=0.0)
            results.append({"recipe_id": recipe_id, "success": False, "error": str(exc)})

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    success_count = sum(1 for r in results if r.get("success"))
    print(f"  完成: {success_count}/{len(results)} case 成功")
    print(f"  Run: {run_name}  (split={split})")
    print(f"  查看结果: Langfuse Dashboard → Sessions → {run_name}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DiagDoctor 后端 Agent 评测：注入→触发→诊断→打分→还原")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个 case")
    parser.add_argument(
        "--cases", type=str, default=None, help="逗号分隔的 recipe_id 列表，如 BE-020,FE-020"
    )
    parser.add_argument(
        "--split",
        type=str,
        default="all",
        choices=["smoke", "train", "all"],
        help="评测子集：smoke=4 个代表性 case（快速冒烟）；train=metadata.split==train；all=全量",
    )
    parser.add_argument(
        "--run-name",
        type=str,
        default=None,
        help="Langfuse run 名（同时作为 session_id 归组本轮 trace）；"
        "不填则自动生成 {split}-YYYYMMDD-HHMMSS",
    )
    args = parser.parse_args()

    # 自动生成 run_name（同时用作 session_id，便于在 Langfuse Sessions 里归组）
    # 注意：Langfuse session_id 字段做前缀匹配，所以 run_name 共享前缀会
    # 导致不同 run 的 trace 在 Sessions 视图里被聚合到一起（你之前的
    # baseline-15case / baseline-15case-pre-fix / baseline-15case-pre-s1 就是
    # 因此被合并显示成 30+ trace）。强制每个 run_name 末尾带 timestamp 后缀，
    # 即使显式传 --run-name 也补上；已带时间戳格式的不重复补。
    ts_suffix = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if args.run_name:
        # 检测是否已含 8 位日期 + 6 位时间格式（YYYYMMDD-HHMMSS）
        import re as _re

        if _re.search(r"\d{8}-\d{6}$", args.run_name):
            run_name = args.run_name
        else:
            run_name = f"{args.run_name}-{ts_suffix}"
    else:
        run_name = f"{args.split}-{ts_suffix}"
    print(f"[run-name] {run_name}  (Langfuse Sessions → {run_name} 查看本轮 trace)")

    # 获取并筛选 dataset items
    dataset = langfuse.get_dataset("diagdoctor-benchmark")
    items = sorted(dataset.items, key=lambda it: it.metadata.get("bug_id", "Z99"))

    # --split 过滤（与 --cases 取交集）
    if args.split == "smoke":
        items = [it for it in items if it.metadata.get("bug_id", "") in SMOKE_CASES]
        print(f"[split=smoke] 限定 {len(SMOKE_CASES)} 个代表性 case: {sorted(SMOKE_CASES)}")
    elif args.split == "train":
        train_items = [it for it in items if it.metadata.get("split") == "train"]
        if train_items:
            items = train_items
            print(f"[split=train] 按 metadata.split==train 过滤 → {len(items)} 个 case")
        else:
            print(
                f"[split=train] 无 case 带 metadata.split 标记，回退为全量 ({len(items)})。"
                " 提示：重跑 import_cases_to_langfuse.py 以写入 split 元数据。"
            )

    if args.cases:
        case_set = {c.strip() for c in args.cases.split(",")}
        items = [it for it in items if it.metadata.get("bug_id", "") in case_set]
        print(f"筛选 case: {args.cases} → {[it.metadata.get('bug_id') for it in items]}")
    if args.limit:
        items = items[: args.limit]

    asyncio.run(main(items=items, run_name=run_name, split=args.split))
