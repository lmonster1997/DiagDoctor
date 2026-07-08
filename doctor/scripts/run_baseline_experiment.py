"""Langfuse 基线 Experiment：注入 Bug → 触发 → 诊断 → 打分 → 恢复。

与 bug-factory 的分工：
  - bug-factory：inject（改代码）+ trigger（发请求）——只"布置考场"
  - Doctor：search_observability（实时查 Loki/Tempo）——自己"收集证据"
  - Experiment：串联上述流程 + 打分 + 恢复现场

运行前确保：
  - demo-app backend 运行在 http://localhost:8000（uvicorn --reload）
  - Doctor API 运行在 http://localhost:8001
  - Loki/Tempo 可访问
  - 当前在 git {BASE_BRANCH} 分支且工作区干净

用法：
    cd doctor && uv run python scripts/run_baseline_experiment.py
    cd doctor && uv run python scripts/run_baseline_experiment.py --limit 3  # 只跑前3个
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
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

# Langfuse 多维度 Scorer（D13 任务 2.1）
from scripts.langfuse_scorers import score_all_dimensions  # noqa: E402
from scripts.langfuse_scorers import score_process_quality  # noqa: E402

# ── 可配置参数 ─────────────────────────────────────────────────────────
DEMO_BACKEND_URL = "http://localhost:8000"
DOCTOR_URL = "http://localhost:8001"
RELOAD_WAIT = 5  # uvicorn reload 等待秒数
DIAGNOSE_TIMEOUT = 12000  # 单次诊断超时秒数
LOKI_INDEX_DELAY = 3  # Loki/Tempo 索引延迟

# ── 评测子集（见 handbook「评测节奏」）──────────────────────────────
# smoke: 4 个代表性 case，覆盖主要类别 + 1 个 smokeless 类。
#   用途 = 改动后快速 catch 灾难性回归 / 验证机制生效，~5min。
#   注意：4 case 无法检测小幅提升，只回答"有没有崩"。
# train / all: 走 Langfuse Dataset 的 metadata.split 字段过滤，
#   无该字段时回退为全量。用于决策点 ablation。
SMOKE_CASES: set[str] = {"BE-020", "FE-020", "PERF-020", "LOGIC-020"}


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


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    """运行命令，失败时抛出异常。返回完整 stdout 供调用方解析。"""
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
    return result.stdout


BASE_BRANCH = "dev-create-agent"


def git_checkout_base() -> None:
    """切换到 base 分支，确保干净起点。"""
    run_cmd(["git", "checkout", BASE_BRANCH], cwd=PROJECT_ROOT.parent)
    print(f"  [OK] 已切换到 {BASE_BRANCH} 分支")


def inject_bug(recipe_id: str) -> None:
    """注入 Bug：修改源码。"""
    run_cmd(
        ["uv", "run", "python", "-m", "bug_factory.cli", "inject", recipe_id],
        cwd=BUG_FACTORY_DIR,
    )


def trigger_bug(recipe_id: str) -> tuple[datetime, list[str]]:
    """触发 Bug：对 demo-app 发起请求，产生日志和 Trace。

    返回 (触发开始时间 UTC, 本次触发关联的 trace_id 列表)。
    trace_id 列表来自 bug-factory CLI 输出的 `TRACE_IDS_JSON=` 行，
    包含注入的 aiohttp trace_id 与 UI 捕获的前端 trace_id，
    供 Doctor 按 trace_id 精准查 Loki/Tempo（取代宽时间窗，避免跨 case 污染）。
    不加 --no-ui，保持真实用户操作路径。
    """
    trigger_start = datetime.now(UTC)
    stdout = run_cmd(
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
    trace_ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("TRACE_IDS_JSON="):
            try:
                payload = json.loads(line[len("TRACE_IDS_JSON="):])
                trace_ids = list(payload.get("trace_ids", []))
            except (ValueError, TypeError):
                pass
            break
    return trigger_start, trace_ids


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
    """完整的"布置考场 → 诊断 → 清理"流水线。"""
    recipe_id = item.metadata.get("recipe_id", "unknown")
    user_report = item.input.get("user_report", "")

    print(f"\n{'=' * 60}")
    print(f"  Case: {recipe_id}")
    print(f"  User Report: {user_report[:80]}...")
    print(f"{'=' * 60}")

    # ── Step 1: 恢复干净起点 ───────────────────────────────────────
    print("[1/4] 恢复 git base 分支...")
    git_checkout_base()

    # ── Step 2: 注入 Bug ──────────────────────────────────────────
    print(f"[2/4] 注入 Bug: {recipe_id}...")
    inject_bug(recipe_id)
    print(f"  等待 uvicorn reload ({RELOAD_WAIT}s)...")
    time.sleep(RELOAD_WAIT)

    if not await wait_for_backend(DEMO_BACKEND_URL):
        raise RuntimeError(f"Demo backend 未在 {RELOAD_WAIT + 30}s 内就绪")
    print("  [OK] Demo backend 已就绪")

    # ── Step 3: 触发 Bug + 记录时间 ───────────────────────────────
    print(f"[3/4] 触发 Bug: {recipe_id}...")
    trigger_time, trace_ids = trigger_bug(recipe_id)
    print(f"  触发时间: {trigger_time.isoformat()}")
    print(f"  关联 trace_ids: {trace_ids or '(无)'}")
    print(f"  等待 Loki/Tempo 索引 ({LOKI_INDEX_DELAY}s)...")
    await asyncio.sleep(LOKI_INDEX_DELAY)

    # ── Step 4: 调用 Doctor（证据由 Doctor 自己实时查询） ─────────
    print("[4/4] 调用 Doctor API 诊断...")
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

    # ── 恢复现场 ──────────────────────────────────────────────────
    print("  恢复 git base 分支...")
    git_checkout_base()
    time.sleep(2)

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

    # 确保在 base 分支
    git_checkout_base()

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

        # 确保恢复 base 分支
        with contextlib.suppress(Exception):
            git_checkout_base()

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    success_count = sum(1 for r in results if r.get("success"))
    print(f"  完成: {success_count}/{len(results)} case 成功")
    print(f"  Run: {run_name}  (split={split})")
    print(f"  查看结果: Langfuse Dashboard → Sessions → {run_name}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DiagDoctor 基线 Experiment")
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
