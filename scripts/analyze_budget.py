"""固化 §6.3 budget 分布查询（§7.4 可观测性闭环）。

从 Langfuse 拉 session 级（或 tag 级）trace，读 ``_finalize_langfuse_trace`` 写入
trace.output 的 budget 字段（tool_calls / total_tokens / early_stopped /
elapsed_seconds / forced_final_json_call），算分布（P50/P75/P90/max）+ early_stop
率 + forced 率。把 §5.3 标定时靠的临时脚本落成可复跑的固化查询。

依赖 Langfuse 在线。trace.output 缺字段的 trace 跳过 + 计数（诚实降级，不静默）。

用法（从项目根或 doctor/ 下都行，脚本自己定位 backend）::

    uv run python scripts/analyze_budget.py --session baseline-15case-20260726-...
    uv run python scripts/analyze_budget.py --session <run_name> --json
    uv run python scripts/analyze_budget.py --tags phase0 --limit 50
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DOCTOR_BACKEND = PROJECT_ROOT / "doctor" / "backend"
sys.path.insert(0, str(DOCTOR_BACKEND))

from src.config import settings  # noqa: E402


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _stats(vals: list[float]) -> dict[str, float]:
    s = sorted(vals)
    return {
        "n": len(s),
        "p50": _percentile(s, 0.50),
        "p75": _percentile(s, 0.75),
        "p90": _percentile(s, 0.90),
        "max": max(s) if s else 0,
        "mean": statistics.mean(s) if s else 0,
    }


def _fetch_traces(lf: Any, *, session_id: str | None, tags: str | None, limit: int) -> list[Any]:
    """Fetch traces from Langfuse, normalizing the SDK's return shape.

    ``Langfuse.get_traces`` may return a high-level list or a low-level ``Traces``
    pager object with a ``.data`` list -- handle both.
    """
    kwargs: dict[str, Any] = {"limit": limit}
    if session_id:
        kwargs["session_id"] = session_id
    if tags:
        kwargs["tags"] = tags
    result = lf.get_traces(**kwargs)
    if hasattr(result, "data"):
        return list(result.data)
    return list(result)


def _parse_trace(trace: Any) -> dict[str, Any] | None:
    """Extract a budget row from a trace. Returns None if output is missing fields."""
    out = getattr(trace, "output", None)
    if not isinstance(out, dict) or "tool_calls" not in out:
        return None
    meta = getattr(trace, "metadata", None) or {}
    name = getattr(trace, "name", "") or ""
    return {
        "recipe_id": meta.get("recipe_id") or name,
        "tool_calls": int(out.get("tool_calls", 0) or 0),
        "total_tokens": int(out.get("total_tokens", 0) or 0),
        "early_stopped": bool(out.get("early_stopped", False)),
        "forced": bool(out.get("forced_final_json_call", False)),
        "elapsed_seconds": round(float(out.get("elapsed_seconds", 0) or 0), 1),
    }


def _build_summary(session: str, traces: list[Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = [r["tool_calls"] for r in rows]
    tokens = [r["total_tokens"] for r in rows]
    elapsed = [r["elapsed_seconds"] for r in rows]
    n = len(rows)
    early_stop_rate = sum(1 for r in rows if r["early_stopped"]) / n if n else 0.0
    forced_rate = sum(1 for r in rows if r["forced"]) / n if n else 0.0
    return {
        "session": session,
        "traces_total": len(traces),
        "traces_parsed": n,
        "traces_skipped": len(traces) - n,
        "tool_calls": _stats(tool_calls),
        "total_tokens": _stats(tokens),
        "elapsed_seconds": _stats(elapsed),
        "early_stop_rate": round(early_stop_rate, 4),
        "forced_final_call_rate": round(forced_rate, 4),
        "per_case": rows,
    }


def _print_table(summary: dict[str, Any]) -> None:
    print(f"session:            {summary['session']}")
    print(
        f"traces:             {summary['traces_parsed']} parsed / "
        f"{summary['traces_total']} total ({summary['traces_skipped']} skipped)"
    )
    print()
    tc = summary["tool_calls"]
    tk = summary["total_tokens"]
    el = summary["elapsed_seconds"]
    print("                n     P50     P75     P90     max    mean")
    print(f"  tool_calls  {tc['n']:<5}  {tc['p50']:<6.0f}  {tc['p75']:<6.0f}  {tc['p90']:<6.0f}  {tc['max']:<6.0f}  {tc['mean']:<.1f}")
    print(f"  total_tokens{tk['n']:<5}  {tk['p50']:<6.0f}  {tk['p75']:<6.0f}  {tk['p90']:<6.0f}  {tk['max']:<6.0f}  {tk['mean']:<.1f}")
    print(f"  elapsed_s   {el['n']:<5}  {el['p50']:<6.0f}  {el['p75']:<6.0f}  {el['p90']:<6.0f}  {el['max']:<6.0f}  {el['mean']:<.1f}")
    print()
    print(f"  early_stop_rate:         {summary['early_stop_rate']:.1%}")
    print(f"  forced_final_call_rate:  {summary['forced_final_call_rate']:.1%}")
    print()
    print("  per-case:")
    print(f"    {'recipe_id':<16} {'tool_calls':>10} {'tokens':>10} {'elapsed':>8} {'early':>6} {'forced':>6}")
    for r in summary["per_case"]:
        rid = str(r["recipe_id"])[:16]
        print(
            f"    {rid:<16} {r['tool_calls']:>10} {r['total_tokens']:>10} "
            f"{r['elapsed_seconds']:>8.1f} {str(r['early_stopped']):>6} {str(r['forced']):>6}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze budget distribution from Langfuse traces.")
    parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="Langfuse session_id (run_name) to filter traces by.",
    )
    parser.add_argument(
        "--tags",
        type=str,
        default=None,
        help="Filter by tag (alternative to --session).",
    )
    parser.add_argument("--limit", type=int, default=100, help="Max traces to fetch (default 100).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    if not args.session and not args.tags:
        parser.error("require --session <run_name> or --tags <tag>")

    from langfuse import Langfuse

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    traces = _fetch_traces(
        lf, session_id=args.session, tags=args.tags, limit=args.limit
    )
    rows = []
    for t in traces:
        row = _parse_trace(t)
        if row is not None:
            rows.append(row)

    summary = _build_summary(args.session or f"tags={args.tags}", traces, rows)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_table(summary)


if __name__ == "__main__":
    main()
