#!/usr/bin/env python3
"""
一键跑通 Bug Case → 诊断 → 评测 → Langfuse 记录。

Usage::

    # 跑指定 Bug Case（默认全流程：注入 → 触发 → 收集 → 诊断 → 评测）
    uv run python scripts/run_case.py BE-020

    # 跳过注入（bug 已在分支上）
    uv run python scripts/run_case.py BE-020 --skip-inject

    # 跳过触发+收集（evidence 已存在）
    uv run python scripts/run_case.py BE-020 --skip-trigger

    # 上传评测结果到 Langfuse Dataset
    uv run python scripts/run_case.py BE-020 --langfuse

    # 只查看已有 case 的详情
    uv run python scripts/run_case.py BE-020 --show-only

流程:
    [0/5] 前置检查 — Doctor API, Demo Backend
    [1/5] Bug 注入    — AI 改写代码 → bug/{id} 分支
    [2/5] 触发+收集   — 触发 bug → Loki/Tempo 收集 → 生成 case.yaml
    [3/5] 诊断        — 调用 Doctor API（直接 HTTP，走完整诊断流程）
    [4/5] 评测        — 本地打分 + LLM Judge
    [5/5] Langfuse    — 上传 dataset item + trace 关联（--langfuse 启用）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 项目路径 ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUG_FACTORY_DIR = PROJECT_ROOT / "bug-factory"
DOCTOR_SRC = PROJECT_ROOT / "doctor" / "src"

# ═══════════════════════════════════════════════════════════════════════
# 终端样式
# ═══════════════════════════════════════════════════════════════════════


class Style:
    """ANSI 终端样式（避免依赖 rich，PowerShell 下稳定）。"""

    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{Style.CYAN}{'=' * 60}{Style.RESET}")
    print(f"{Style.CYAN}{Style.BOLD}  {text}{Style.RESET}")
    print(f"{Style.CYAN}{'=' * 60}{Style.RESET}\n")


def step(num: int, total: int, text: str) -> None:
    print(f"{Style.MAGENTA}[{num}/{total}] {text}...{Style.RESET}")


def ok(text: str) -> None:
    print(f"  {Style.GREEN}✓ {text}{Style.RESET}")


def warn(text: str) -> None:
    print(f"  {Style.YELLOW}⚠ {text}{Style.RESET}")


def fail(text: str) -> None:
    print(f"  {Style.RED}✗ {text}{Style.RESET}")


def dim(text: str) -> None:
    print(f"  {Style.DIM}{text}{Style.RESET}")


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════


def run_cmd(
    cmd: list[str], cwd: Path | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    """运行命令并实时输出，失败时退出。"""
    full_env = os.environ.copy()
    full_env["PYTHONIOENCODING"] = "utf-8"
    if env:
        full_env.update(env)

    print(f"  {Style.DIM}> {' '.join(cmd)}{Style.RESET}")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=False,
        text=True,
    )
    if proc.returncode != 0:
        fail(f"命令失败 (exit={proc.returncode}): {' '.join(cmd)}")
        sys.exit(proc.returncode)
    return proc


def check_http(url: str, label: str) -> bool:
    """检查 HTTP 端点。"""
    try:
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=5)
        ok(f"{label} 已运行 ({url})")
        return True
    except Exception:
        fail(f"{label} 未响应！请先启动: {url}")
        return False


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════
# Step 3: 诊断 — 调用 Doctor API（HTTP）
# ═══════════════════════════════════════════════════════════════════════


async def diagnose_case(
    bug_id: str,
    doctor_url: str,
    timeout: int = 300,
) -> dict[str, Any]:
    """调用 Doctor API 诊断一个 case，返回完整响应。"""
    import aiohttp
    import yaml

    case_dir = BUG_FACTORY_DIR / "output" / bug_id
    evidence_dir = case_dir / "evidence"

    case_yaml = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    user_report = case_yaml.get("input", {}).get("user_report", "")

    logs = json.loads((evidence_dir / "logs.json").read_text(encoding="utf-8"))
    traces = json.loads((evidence_dir / "traces.json").read_text(encoding="utf-8"))
    browser_path = evidence_dir / "browser_errors.json"
    browser_errors = (
        json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else []
    )

    payload = {
        "evidence": {
            "user_report": user_report,
            "logs": logs,
            "traces": traces,
            "browser_errors": browser_errors,
        }
    }

    print(
        f"  Evidence: {len(logs)} logs, {len(traces)} traces, {len(browser_errors)} browser_errors"
    )
    print(f"  User report: {user_report[:120]}...")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{doctor_url}/api/diagnose",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                fail(f"Doctor API 返回 {resp.status}: {text[:500]}")
                return {"error": text, "status": resp.status}
            return await resp.json()


# ═══════════════════════════════════════════════════════════════════════
# Step 4: 本地评测打分
# ═══════════════════════════════════════════════════════════════════════


def evaluate_locally(
    diagnosis: dict[str, Any],
    bug_id: str,
) -> dict[str, Any]:
    """基于 case.yaml 的 expected 字段做本地结构化打分。"""
    import yaml

    case_yaml_path = BUG_FACTORY_DIR / "output" / bug_id / "case.yaml"
    if not case_yaml_path.exists():
        warn("case.yaml 不存在，跳过评测")
        return {"scores": {}, "overall": 0.0, "details": ["case.yaml not found"]}

    case_yaml = yaml.safe_load(case_yaml_path.read_text(encoding="utf-8"))
    expected = case_yaml.get("expected", {})

    report = diagnosis.get("report") or {}
    findings_count = diagnosis.get("findings_count", 0)

    scores: dict[str, float] = {}
    details: list[str] = []

    # 1. Finding 不为空
    finding_ok = findings_count > 0
    scores["has_findings"] = 1.0 if finding_ok else 0.0
    if not finding_ok:
        details.append("❌ 无 finding")

    # 2. 分类正确性
    expected_categories = expected.get("categories", [])
    if expected_categories:
        categories = diagnosis.get("categories", [])
        match = any(ec in categories for ec in expected_categories)
        scores["category_match"] = 1.0 if match else 0.0
        if not match:
            details.append(f"⚠️ category mismatch: got={categories}, expected={expected_categories}")
    else:
        scores["category_match"] = 0.5  # 无期望值

    # 3. root_cause_tier 匹配
    expected_tier = expected.get("root_cause_tier", "")
    if expected_tier and report:
        got_tier = report.get("root_cause_tier", "")
        scores["tier_match"] = 1.0 if got_tier == expected_tier else 0.0
        if got_tier != expected_tier:
            details.append(f"⚠️ tier mismatch: got={got_tier}, expected={expected_tier}")

    # 4. Confidence
    if report:
        scores["confidence"] = min(report.get("confidence", 0), 1.0)

    # 5. Affected file 命中
    expected_files = expected.get("affected_files", [])
    if expected_files and report:
        got_file = report.get("affected_file", "")
        hits = sum(1 for ef in expected_files if ef in got_file or got_file in ef)
        scores["file_match"] = hits / len(expected_files)

    # ── 加权综合 ──
    weights = {
        "has_findings": 0.20,
        "category_match": 0.30,
        "tier_match": 0.20,
        "confidence": 0.10,
        "file_match": 0.20,
    }
    overall = sum(scores.get(k, 0.0) * w for k, w in weights.items())

    return {"scores": scores, "overall": overall, "details": details}


# ═══════════════════════════════════════════════════════════════════════
# Step 5: Langfuse 集成（上传 dataset item + score）
# ═══════════════════════════════════════════════════════════════════════


def upload_to_langfuse(
    bug_id: str,
    diagnosis: dict[str, Any],
    eval_result: dict[str, Any],
) -> bool:
    """
    将评测结果上传到 Langfuse。

    数据集: ``diagdoctor-benchmark``
    Item ID = bug_id，支持幂等覆盖。
    同时上传 score 到对应的 trace。

    前置条件: doctor/.env 中配置了 langfuse_secret_key / langfuse_public_key。
    """
    try:
        from langfuse import Langfuse
    except ImportError:
        warn("langfuse 未安装，跳过上传。pip install langfuse")
        return False

    sys.path.insert(0, str(DOCTOR_SRC))
    try:
        from src.config import settings
    except Exception:
        warn("无法加载 doctor 配置，跳过 Langfuse")
        return False

    if not settings.langfuse_secret_key or not settings.langfuse_public_key:
        warn("Langfuse 未配置 (secret_key/public_key 为空)，跳过")
        return False

    import yaml

    case_yaml_path = BUG_FACTORY_DIR / "output" / bug_id / "case.yaml"
    case_yaml = (
        yaml.safe_load(case_yaml_path.read_text(encoding="utf-8"))
        if case_yaml_path.exists()
        else {}
    )

    client = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    dataset_name = "diagdoctor-benchmark"

    # 确保 dataset 存在
    try:
        client.get_dataset(dataset_name)
    except Exception:
        client.create_dataset(name=dataset_name)
        ok(f"Langfuse dataset 已创建: {dataset_name}")

    # 构建 input（证据摘要）
    input_data = {
        "bug_id": bug_id,
        "user_report": case_yaml.get("input", {}).get("user_report", ""),
    }

    metadata = {
        "bug_id": bug_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diagnosis_trace_id": diagnosis.get("thread_id", ""),
        "eval_scores": eval_result.get("scores", {}),
        "eval_overall": eval_result.get("overall", 0.0),
    }

    try:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=bug_id,
            input=input_data,
            expected_output=case_yaml.get("expected", {}),
            metadata=metadata,
        )
        ok(f"Langfuse dataset item: {bug_id}")

        # 上传 score
        thread_id = diagnosis.get("thread_id", "")
        if thread_id:
            client.score(
                trace_id=thread_id,
                name="overall",
                value=eval_result.get("overall", 0.0),
                comment=f"Auto-evaluated for {bug_id}",
            )
            ok(f"Langfuse score: overall={eval_result.get('overall', 0):.2f}")

        client.flush()
        return True
    except Exception as e:
        warn(f"Langfuse 上传失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DiagDoctor 单 Case 诊断 + 评测 + Langfuse 记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "bug_id",
        nargs="?",
        default=None,
        help="Bug 配方 ID（如 BE-020, FE-020）",
    )
    parser.add_argument("--demo-url", default="http://localhost:8000", help="Demo Backend 地址")
    parser.add_argument("--doctor-url", default="http://127.0.0.1:8001", help="Doctor API 地址")
    parser.add_argument("--reload-wait", type=int, default=5, help="uvicorn reload 等待秒数")
    parser.add_argument("--skip-inject", action="store_true", help="跳过注入（bug 已在分支上）")
    parser.add_argument(
        "--skip-trigger", action="store_true", help="跳过触发+收集（evidence 已存在）"
    )
    parser.add_argument("--no-llm-judge", action="store_true", help="跳过 LLM Judge")
    parser.add_argument("--langfuse", action="store_true", help="上传评测结果到 Langfuse")
    parser.add_argument("--show-only", action="store_true", help="只查看已有 case 详情")

    args = parser.parse_args()

    # ── show-only 模式 ─────────────────────────────────────────────
    if args.show_only:
        if not args.bug_id:
            fail("--show-only 需要指定 bug_id")
            sys.exit(1)
        _cmd_show(args.bug_id)
        return

    if not args.bug_id:
        parser.print_help()
        sys.exit(1)

    bug_id: str = args.bug_id
    total_steps = 5

    header(f"DiagDoctor 单 Case 诊断流水线 — {bug_id}")
    total_start = time.monotonic()

    # ═══════════════════════════════════════════════════════════════
    # Step 0: 前置检查
    # ═══════════════════════════════════════════════════════════════
    step(0, total_steps, "前置检查")

    if not check_http(f"{args.doctor_url}/health", "Doctor API"):
        dim("  启动: cd doctor; uv run uvicorn src.main:app --port 8001 --reload")
        sys.exit(1)

    if not check_http(f"{args.demo_url}/health", "Demo Backend"):
        dim(
            "  启动: cd demo-app/backend; uv run uvicorn app.main:app --reload "
            "--host 127.0.0.1 --port 8000"
        )
        sys.exit(1)

    # ═══════════════════════════════════════════════════════════════
    # Step 1: Bug 注入
    # ═══════════════════════════════════════════════════════════════
    if not args.skip_inject:
        step(1, total_steps, f"Bug Factory: 注入 Bug ({bug_id})")
        t0 = time.monotonic()
        run_cmd(
            ["uv", "run", "python", "-m", "bug_factory.cli", "inject", bug_id],
            cwd=BUG_FACTORY_DIR,
        )
        ok(f"注入完成 ({time.monotonic() - t0:.1f}s)")

        dim(f"等待 uvicorn reload ({args.reload_wait}s)...")
        time.sleep(args.reload_wait)
        check_http(f"{args.demo_url}/health", "Demo Backend")
    else:
        step(1, total_steps, "跳过注入 (--skip-inject)")

    # ═══════════════════════════════════════════════════════════════
    # Step 2: 触发 + 收集 + 生成 case.yaml
    # ═══════════════════════════════════════════════════════════════
    if not args.skip_trigger:
        step(2, total_steps, "Bug Factory: 触发 → 收集 → 生成 case.yaml")
        dim("  这可能需要 1-2 分钟（含 OTel 管道 flush 等待）...")
        t0 = time.monotonic()

        cmd = [
            "uv",
            "run",
            "python",
            "-m",
            "bug_factory.cli",
            "full",
            bug_id,
            "--skip-inject",
            "--clear-loki",
            "--base-url",
            args.demo_url,
        ]
        run_cmd(cmd, cwd=BUG_FACTORY_DIR)
        ok(f"触发+收集+生成 完成 ({time.monotonic() - t0:.1f}s)")
    else:
        step(2, total_steps, "跳过触发+收集 (--skip-trigger)")

    # ═══════════════════════════════════════════════════════════════
    # Step 3: 诊断 — 调用 Doctor API
    # ═══════════════════════════════════════════════════════════════
    step(3, total_steps, "诊断: 调用 Doctor API")
    t0 = time.monotonic()

    diagnosis = asyncio.run(diagnose_case(bug_id, args.doctor_url))

    elapsed = time.monotonic() - t0
    thread_id = diagnosis.get("thread_id", "N/A")
    primary = diagnosis.get("primary_category", "N/A")
    findings = diagnosis.get("findings_count", 0)

    ok(f"诊断完成 ({elapsed:.1f}s)")
    print(f"  Thread ID:      {thread_id}")
    print(f"  Primary:        {primary}")
    print(f"  Findings:       {findings}")

    report = diagnosis.get("report") or {}
    if report:
        print(f"  Root cause:     {str(report.get('root_cause', ''))[:200]}")
        print(f"  Affected file:  {report.get('affected_file', 'N/A')}")
        print(f"  Confidence:     {report.get('confidence', 0):.2%}")

    # ═══════════════════════════════════════════════════════════════
    # Step 4: 评测
    # ═══════════════════════════════════════════════════════════════
    step(4, total_steps, "评测: 本地打分")

    eval_result = evaluate_locally(diagnosis, bug_id)

    print("\n  ── Scores ──")
    for k, v in eval_result["scores"].items():
        print(f"    {k}: {v:.2f}")
    print(f"    {Style.BOLD}OVERALL: {eval_result['overall']:.2f}{Style.RESET}")

    for detail in eval_result.get("details", []):
        print(f"  {detail}")

    # ═══════════════════════════════════════════════════════════════
    # Step 5: Langfuse 上传（可选）
    # ═══════════════════════════════════════════════════════════════
    if args.langfuse:
        step(5, total_steps, "Langfuse: 上传评测结果")
        upload_to_langfuse(bug_id, diagnosis, eval_result)
    else:
        step(5, total_steps, "Langfuse: 跳过 (--langfuse 未启用)")
        dim("  使用 --langfuse 上传评测结果到 Langfuse")
        if thread_id and thread_id != "N/A":
            dim(f"  诊断 Trace: http://127.0.0.1:3002 (trace_id={thread_id})")

    # ── 总结 ─────────────────────────────────────────────────────
    total_elapsed = time.monotonic() - total_start
    header(f"完成 — overall={eval_result['overall']:.2f} | 耗时 {total_elapsed:.0f}s")

    # 提示切回 main
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    if branch != "main":
        warn(f"当前分支: {branch} — 记得切回: git checkout main")


# ═══════════════════════════════════════════════════════════════════════
# show-only: 查看已有 case 详情
# ═══════════════════════════════════════════════════════════════════════


def _cmd_show(bug_id: str) -> None:
    """展示已有 case 的详情（不跑流程）。"""
    import yaml

    case_dir = BUG_FACTORY_DIR / "output" / bug_id
    case_yaml_path = case_dir / "case.yaml"

    if not case_yaml_path.exists():
        fail(f"Case 不存在: {case_yaml_path}")
        sys.exit(1)

    case_yaml = yaml.safe_load(case_yaml_path.read_text(encoding="utf-8"))

    print(f"\n{Style.BOLD}Case: {bug_id}{Style.RESET}")
    print(f"  描述: {case_yaml.get('description', 'N/A')}")
    print(f"  分类: {case_yaml.get('category', 'N/A')}")

    inp = case_yaml.get("input", {})
    print(f"\n{Style.BOLD}Input:{Style.RESET}")
    print(f"  user_report: {inp.get('user_report', 'N/A')[:200]}")

    exp = case_yaml.get("expected", {})
    print(f"\n{Style.BOLD}Expected:{Style.RESET}")
    print(f"  categories: {exp.get('categories', [])}")
    print(f"  root_cause_tier: {exp.get('root_cause_tier', 'N/A')}")
    print(f"  affected_files: {exp.get('affected_files', [])}")
    print(f"  root_cause_summary: {exp.get('root_cause_summary', 'N/A')[:200]}")

    evidence_dir = case_dir / "evidence"
    if evidence_dir.exists():
        logs = load_json(evidence_dir / "logs.json")
        traces = load_json(evidence_dir / "traces.json")
        browser = load_json(evidence_dir / "browser_errors.json")
        print(f"\n{Style.BOLD}Evidence:{Style.RESET}")
        print(f"  logs: {len(logs) if logs else 0} entries")
        print(f"  traces: {len(traces) if traces else 0} spans")
        print(f"  browser_errors: {len(browser) if browser else 0} entries")


if __name__ == "__main__":
    main()
