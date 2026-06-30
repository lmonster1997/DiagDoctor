"""
VS Code 调试脚本 — 加载 bug case 并精确走 Doctor API 的代码路径。

与 ``POST /api/diagnose`` 走完全相同的 ``_build_initial_state``
+ ``_run_graph``，可以直接在 doctor 源码中设断点调试，无需启动 HTTP 服务。

Usage::

    # 默认跑第一个可用的 case
    uv run python scripts/debug_case.py

    # 指定 case
    uv run python scripts/debug_case.py BE-020

    # 只跑 Ingest 节点（不调 LLM，快速验证 evidence 质量）
    uv run python scripts/debug_case.py BE-020 --no-llm

    # 在 _run_graph 前进入 pdb 断点（VS Code 可直接 F5 附加）
    uv run python scripts/debug_case.py BE-020 --debug

    # 直接用文字描述（跳过 case 文件）
    uv run python scripts/debug_case.py --user-report "创建评论返回500错误"

    # 列出所有可用的 case
    uv run python scripts/debug_case.py --list-cases

与 benchmark runner 的关键差异:
    - benchmark  →  HTTP POST :8001/api/diagnose → FastAPI → _run_graph()
    - debug_case  →  直接调用 _build_initial_state() + _run_graph()
    两者经过完全相同的 state 构建 + graph 执行逻辑，只是跳过 HTTP 层。
    因此在这里设断点 = 在 Doctor API 内部设断点。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# ═════════════════════════════════════════════════════════════════════
# 关键：切换到 doctor/ 目录，确保 Pydantic Settings 加载 doctor/.env
# （与 Doctor API server 使用相同的 LLM API key / base_url）
# ═════════════════════════════════════════════════════════════════════
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_DIR = REPO_ROOT / "doctor"
DOCTOR_SRC = DOCTOR_DIR / "src"
BUG_FACTORY_OUTPUT = REPO_ROOT / "bug-factory" / "output"

os.chdir(str(DOCTOR_DIR))  # Settings(env_file=".env") 会加载 doctor/.env

if str(DOCTOR_SRC) not in sys.path:
    sys.path.insert(0, str(DOCTOR_SRC))


# ── Helpers ──────────────────────────────────────────────────────────


def list_available_cases() -> list[str]:
    """列出 bug-factory/output 下所有有 evidence 的 case。"""
    if not BUG_FACTORY_OUTPUT.exists():
        return []
    cases = []
    for d in sorted(BUG_FACTORY_OUTPUT.iterdir()):
        if d.is_dir() and (d / "case.yaml").exists() and (d / "evidence").is_dir():
            cases.append(d.name)
    return cases


def load_case(case_id: str) -> dict[str, Any]:
    """加载 bug-factory/output/{case_id} 的证据文件。"""
    case_dir = BUG_FACTORY_OUTPUT / case_id
    evidence_dir = case_dir / "evidence"

    if not case_dir.exists():
        print(f"[ERROR] Case 目录不存在: {case_dir}")
        available = list_available_cases()
        if available:
            print(f"  可用的 case: {', '.join(available)}")
        sys.exit(1)

    import yaml

    case_yaml = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    user_report = case_yaml.get("input", {}).get("user_report", "")
    expected = case_yaml.get("expected", {})

    logs = json.loads((evidence_dir / "logs.json").read_text(encoding="utf-8"))
    traces = json.loads((evidence_dir / "traces.json").read_text(encoding="utf-8"))
    browser_path = evidence_dir / "browser_errors.json"
    browser_errors = (
        json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else []
    )

    return {
        "case_id": case_id,
        "user_report": user_report,
        "logs": logs,
        "traces": traces,
        "browser_errors": browser_errors,
        "expected": expected,
    }


def print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Main debug flow ─────────────────────────────────────────────────


async def run_case(
    case_id: str = "",
    *,
    user_report: str = "",
    skip_llm: bool = False,
    debug: bool = False,
) -> None:
    """
    加载 case 并通过与 Doctor API 完全相同的代码路径运行诊断。

    使用 ``src.api.diagnose._build_initial_state`` + ``_run_graph``。
    """
    from src.api.diagnose import DiagnoseRequest, _build_initial_state, _run_graph
    from src.config import settings
    from src.graph.main_graph import generate_thread_id
    from src.graph.state import BrowserError, Evidence, LogEntry, TraceSpan

    # ── 加载证据 ──────────────────────────────────────────────
    if user_report:
        # 纯文本模式：构造最小 evidence
        print_section("Raw text mode (no case file)")
        print(f"  user_report: {user_report[:200]}")
        logs, traces, browser_errors = [], [], []
        expected: dict[str, Any] = {}
    else:
        print_section(f"Step 1: Loading case {case_id}")
        data = load_case(case_id)
        user_report = data["user_report"]
        logs = data["logs"]
        traces = data["traces"]
        browser_errors = data["browser_errors"]
        expected = data["expected"]

        print(f"  user_report: {user_report[:120]}...")
        print(f"  logs: {len(logs)} entries")
        print(f"  traces: {len(traces)} spans")
        print(f"  browser_errors: {len(browser_errors)} entries")
        if expected:
            print(f"  expected.categories: {expected.get('categories', [])}")
            print(f"  expected.root_cause_tier: {expected.get('root_cause_tier', '?')}")

    # ── 数据质量速览 ──────────────────────────────────────────
    if logs or traces:
        print_section("Data quality quick check")
        log_tids = {log.get("trace_id") for log in logs if log.get("trace_id")}
        span_tids = {span.get("trace_id") for span in traces if span.get("trace_id")}
        print(f"  logs with trace_id:  {len(log_tids)}/{len(logs)}")
        print(f"  spans with trace_id: {len(span_tids)}/{len(traces)}")
        print(f"  trace_id overlap:    {len(log_tids & span_tids)}")

        levels: dict[str, int] = {}
        for log in logs:
            lvl = log.get("level", log.get("detected_level", "INFO"))
            levels[lvl] = levels.get(lvl, 0) + 1
        print(f"  log levels: {levels}")

        span_services = {span.get("service_name", span.get("service", "")) for span in traces}
        print(f"  span services: {span_services}")

    # ── 构建 Evidence → DiagnoseRequest ───────────────────────
    print_section("Step 2: Building DiagnoseRequest (same as API)")

    def _sanitize(d: dict, model_cls: type) -> dict:
        """Keep only fields known to the Pydantic model."""
        known_fields = set(model_cls.model_fields.keys())
        clean: dict[str, Any] = {}
        for k, v in d.items():
            if k not in known_fields:
                if k == "operation_name" and "name" in known_fields:
                    k = "name"
                elif k == "start_time" and "start" in known_fields:
                    k = "start"
                elif k == "line" and "message" in known_fields:
                    k = "message"
                else:
                    continue
            if v is None:
                info = model_cls.model_fields.get(k)
                if info and info.annotation is not None:
                    origin = getattr(info.annotation, "__origin__", None)
                    clean[k] = [] if origin is list else ""
                else:
                    clean[k] = ""
            elif isinstance(v, list):
                clean[k] = [item if isinstance(item, dict) else {"raw": str(item)} for item in v]
            else:
                clean[k] = v
        return clean

    evidence = Evidence(
        user_report=user_report,
        logs=[LogEntry(**_sanitize(log, LogEntry)) for log in logs],
        traces=[TraceSpan(**_sanitize(span, TraceSpan)) for span in traces],
        browser_errors=[BrowserError(**_sanitize(err, BrowserError)) for err in browser_errors],
    )

    request = DiagnoseRequest(evidence=evidence)

    print(f"  LLM: {settings.llm_model} @ {settings.llm_base_url}")

    # ── Ingest-only mode ───────────────────────────────────────
    if skip_llm:
        print_section("Step 3: Ingest only (--no-llm)")
        from src.graph.nodes.ingest import ingest_node
        from src.graph.state import DoctorState

        state = DoctorState(raw_evidence=evidence, case_id=case_id or "debug")
        ingest_result = await ingest_node(state)
        normalized = ingest_result["evidence"]

        print(f"\n  golden_signals ({len(normalized.golden_signals)}):")
        for sig in normalized.golden_signals:
            tier_emoji = "🖥" if sig.service_tier == "frontend" else "🖧"
            sev = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
            print(f"    {sev} [{tier_emoji}] [{sig.signal_type}] id={sig.signal_id}")
            print(f"       summary: {sig.summary[:160]}")

        print(f"\n  correlations ({len(normalized.correlations)}):")
        for c in normalized.correlations:
            print(f"    [{c.correlation_id}] trace={c.trace_id} conf={c.confidence:.2f}")
            print(f"      frontend_signals: {c.frontend_signals}")
            print(f"      backend_signals:  {c.backend_signals}")

        print(f"\n  noise_ratio: {normalized.noise_ratio:.2%}")
        print(f"  frontend_span_count: {normalized.frontend_span_count}")
        print(f"  backend_span_count:  {normalized.backend_span_count}")
        return

    # ── Step 3: Run FULL graph ─────────────────────────────────
    print_section("Step 3: Running FULL graph (same as POST /api/diagnose)")
    print(f"  CWD: {os.getcwd()}  ← 确保加载 doctor/.env")
    print()

    thread_id = generate_thread_id()

    # 🔴 这两行就是 Doctor API 的核心逻辑 🔴
    initial_state = _build_initial_state(request, thread_id)

    if debug:
        print("[DEBUG] 进入 pdb — 在 doctor 源码设断点后按 c 继续")
        print("[DEBUG] 推荐断点位置:")
        print("        src/graph/nodes/unified_agent.py → unified_agent_node")
        print("        src/graph/nodes/ingest.py → ingest_node")
        breakpoint()  # ← VS Code: 在此设断点或 F5 附加后按 c

    final_state = await _run_graph(thread_id, initial_state)

    # ── Results ────────────────────────────────────────────────
    print_section("RESULTS")

    report = final_state.get("report")
    findings = final_state.get("findings", [])

    # Ingest output
    normalized = final_state.get("evidence")
    if normalized:
        print(f"\n[Ingest] golden_signals: {len(normalized.golden_signals)}")
        print(f"[Ingest] correlations:   {len(normalized.correlations)}")
        print(f"[Ingest] noise_ratio:     {normalized.noise_ratio:.2%}")

    # Agent output
    if report:
        print(f"\n[Report] primary_category: {report.primary_category}")
        print(f"[Report] categories:       {report.categories}")
        print(f"[Report] root_cause:       {report.root_cause[:300]}")
        print(f"[Report] fix_suggestion:   {report.fix_suggestion[:300]}")
        print(f"[Report] affected_file:    {report.affected_file}")
        print(f"[Report] confidence:       {report.confidence}")
        print(f"[Report] symptom_tier:     {report.symptom_tier}")
        print(f"[Report] root_cause_tier:  {report.root_cause_tier}")
        print(f"[Report] early_stopped:    {report.early_stopped}")
        if report.notes:
            print(f"[Report] notes:            {report.notes[:200]}")

    # Findings
    print(f"\n[Findings] count: {len(findings)}")
    for i, f in enumerate(findings):
        print(f"  [{i}] agent={f.agent}, confidence={f.confidence:.2f}")
        print(f"      summary:        {f.summary[:200]}")
        print(f"      affected_files: {f.affected_files}")
        print(f"      evidence_refs:  {f.evidence_refs}")

    # Budget
    budget = final_state.get("budget")
    if budget:
        print(
            f"\n[Budget] tool_calls={budget.tool_calls}, "
            f"tokens={budget.total_tokens}, "
            f"elapsed={budget.elapsed_seconds:.1f}s"
        )


# ── CLI entry ────────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DiagDoctor VS Code 调试 — 直接走 Doctor API 代码路径",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run python scripts/debug_case.py BE-020
  uv run python scripts/debug_case.py BE-020 --no-llm
  uv run python scripts/debug_case.py BE-020 --debug
  uv run python scripts/debug_case.py --user-report "创建评论返回500"
  uv run python scripts/debug_case.py --list-cases
        """,
    )
    parser.add_argument(
        "case_id",
        nargs="?",
        default=None,
        help="Bug case ID（如 BE-020），不传则用第一个可用 case",
    )
    parser.add_argument(
        "--no-llm",
        "--ingest-only",
        action="store_true",
        dest="no_llm",
        help="只跑 Ingest 节点（不调 LLM，快速验证 evidence）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="在 _run_graph 前进入 pdb 断点",
    )
    parser.add_argument(
        "--user-report",
        type=str,
        default="",
        help="直接用文字描述（跳过 case 文件），用于快速实验",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="列出 bug-factory/output 下所有可用 case",
    )

    args = parser.parse_args()

    # ── list-cases ──────────────────────────────────────────────
    if args.list_cases:
        cases = list_available_cases()
        if cases:
            print(f"可用的 case ({len(cases)}):")
            for c in cases:
                print(f"  {c}")
        else:
            print("bug-factory/output/ 下没有可用的 case。")
            print(
                "请先运行 Bug Factory 生成 case: cd bug-factory && "
                "uv run python -m bug_factory.cli full BE-020"
            )
        sys.exit(0)

    # ── 确定 case_id ────────────────────────────────────────────
    case_id = args.case_id or ""
    user_report = args.user_report

    if not user_report and not case_id:
        # 自动选第一个可用 case
        available = list_available_cases()
        if available:
            case_id = available[0]
        else:
            print("[ERROR] 没有可用的 case，且未指定 --user-report。")
            print("请先运行 Bug Factory 或使用 --user-report。")
            sys.exit(1)

    print(f"╔{'═' * 58}╗")
    label = f"case: {case_id}" if case_id else "raw text mode"
    print(f"║  DiagDoctor Debug — {label:<34s} ║")
    print("║  CWD: doctor/  →  加载 doctor/.env               ║")
    print(f"╚{'═' * 58}╝")
    if args.no_llm:
        print("[MODE] Ingest-only (no LLM)")
    if args.debug:
        print("[MODE] Debug — 将在 _run_graph 前进入 pdb")
    if user_report:
        print(f"[MODE] Raw text — {user_report[:60]}...")

    asyncio.run(
        run_case(
            case_id=case_id,
            user_report=user_report,
            skip_llm=args.no_llm,
            debug=args.debug,
        )
    )
