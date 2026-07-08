"""Dump 一个 Langfuse session 的全部 trace 分数表（含 overall 与 7 维度 + process_quality）。

常用于 baseline / ablation 跑完后快速拉一张分数表 + 聚合统计。

Usage:
    uv run python scripts/dump_session_scores.py <session_id>
    uv run python scripts/dump_session_scores.py baseline-15case-pres1

说明：
- session_id 即 run_baseline_experiment.py 的 --run-name（同时作为 Langfuse session_id）。
- 同名 score 可能有历史多条（rescore 后），按 timestamp 取最新一条。
- 维度列：root=root_cause_accuracy, cat=category_accuracy, file=affected_file_accuracy,
  fix=fix_suggestion_quality, line=affected_line_accuracy, evid=evidence_chain_completeness,
  conf=confidence_calibration, proc=process_quality（不计入 overall）。
"""

from __future__ import annotations

import os
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402


def _fetch_session_traces(lf: Langfuse, session_id: str) -> list:
    """分页拉取一个 session 的全部 trace。"""
    traces: list = []
    page = 1
    page_size = 100
    while True:
        resp = lf.fetch_traces(session_id=session_id, page=page, limit=page_size)
        batch = getattr(resp, "data", []) or []
        traces.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return traces


def _latest_scores(lf: Langfuse, trace) -> dict[str, float | None]:
    """取一条 trace 的全部 score，同名按 timestamp 取最新。返回 {name: value}。"""
    full = lf.fetch_trace(trace.id).data
    latest_by_name: dict[str, object] = {}
    for s in full.scores or []:
        cur = latest_by_name.get(s.name)
        ts_s = getattr(s, "timestamp", None)
        ts_c = getattr(cur, "timestamp", None) if cur is not None else None
        if cur is None or (ts_s and ts_c and ts_s > ts_c):
            latest_by_name[s.name] = s
    return {name: getattr(s, "value", None) for name, s in latest_by_name.items()}


def _bug_id(trace) -> str:
    """从 trace name 或 metadata 提取 bug_id。"""
    md = trace.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or trace.name.split("_")[-1]


def _fmt(v, width: int = 6) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.2f}".rjust(width)
    return "  - ".rjust(width)


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else "baseline-15case-pres1"

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    traces = _fetch_session_traces(lf, session_id)
    print(f"# Session: {session_id}  ({len(traces)} traces)\n")

    rows: list[dict] = []
    for t in traces:
        scores = _latest_scores(lf, t)
        rows.append(
            {
                "bug_id": _bug_id(t),
                "overall": scores.get("overall"),
                "root": scores.get("root_cause_accuracy"),
                "cat": scores.get("category_accuracy"),
                "file": scores.get("affected_file_accuracy"),
                "fix": scores.get("fix_suggestion_quality"),
                "line": scores.get("affected_line_accuracy"),
                "evid": scores.get("evidence_chain_completeness"),
                "conf": scores.get("confidence_calibration"),
                "proc": scores.get("process_quality"),
            }
        )
    rows.sort(key=lambda r: str(r["bug_id"]))

    # ── 分数表 ──
    hdr = (
        f"{'bug_id':<13}{'overall':>8}{'root':>7}{'cat':>6}{'file':>6}"
        f"{'fix':>6}{'line':>6}{'evid':>6}{'conf':>6}{'proc':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['bug_id']:<13}{_fmt(r['overall'], 8)}{_fmt(r['root'], 7)}"
            f"{_fmt(r['cat'], 6)}{_fmt(r['file'], 6)}{_fmt(r['fix'], 6)}"
            f"{_fmt(r['line'], 6)}{_fmt(r['evid'], 6)}{_fmt(r['conf'], 6)}"
            f"{_fmt(r['proc'], 7)}"
        )

    # ── 聚合统计 ──
    print("\n# Aggregates (overall)")
    nums = [r["overall"] for r in rows if isinstance(r["overall"], (int, float))]
    if nums:
        print(f"  mean={sum(nums) / len(nums):.3f}  n={len(nums)}")
        print(f"  min={min(nums):.2f}  max={max(nums):.2f}")
        disasters = [
            r["bug_id"]
            for r in rows
            if isinstance(r["overall"], (int, float)) and r["overall"] < 0.4
        ]
        print(f"  disasters (overall<0.4): {len(disasters)} -> {disasters}")

    # ── 各维度均值（便于看哪个维度拉后腿）──
    print("\n# Per-dimension mean")
    dims = ["root", "cat", "file", "fix", "line", "evid", "conf", "proc"]
    for d in dims:
        vals = [r[d] for r in rows if isinstance(r[d], (int, float))]
        if vals:
            print(f"  {d:<6} mean={sum(vals) / len(vals):.3f}  n={len(vals)}")
        else:
            print(f"  {d:<6} (no data)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
