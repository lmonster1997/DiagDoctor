"""Dump all observations of a single trace (by trace_id or by session+bug_id).

Usage:
    uv run python scripts/dump_trace_observations.py <trace_id>
    uv run python scripts/dump_trace_observations.py --session <session_id> --bug <bug_id>
"""
from __future__ import annotations

import argparse
import os
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402


def _fetch_session_traces(lf: Langfuse, session_id: str) -> list:
    traces: list = []
    page = 1
    while True:
        resp = lf.fetch_traces(session_id=session_id, page=page, limit=100)
        batch = getattr(resp, "data", []) or []
        traces.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return traces


def _bug_id(trace) -> str:
    md = trace.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or trace.name.split("_")[-1]


def _walk_observations(obs_list, depth=0, out=None):
    """Recursively walk observations (Langfuse nests via .children)."""
    if out is None:
        out = []
    for obs in obs_list:
        out.append((depth, obs))
        children = getattr(obs, "children", None) or []
        if children:
            _walk_observations(children, depth + 1, out)
    return out


def _print_obs(flat, trace_name, trace_id):
    print(f"\n# Trace: {trace_name}\n# id: {trace_id}\n")
    total = len(flat)
    by_type: dict[str, int] = {}
    tool_calls = 0
    for depth, obs in flat:
        t = getattr(obs, "type", "?") or "?"
        by_type[t] = by_type.get(t, 0) + 1
        # tool-call heuristic: type=='INPUT' ish — actually Langfuse spans; we use name + presence of input/output
    # Heuristic: count observations whose name suggests a tool call.
    for depth, obs in flat:
        nm = (getattr(obs, "name", "") or "").lower()
        if any(
            k in nm
            for k in (
                "tool", "search_observability", "search_tempo", "loki",
                "code_search", "read", "db_query", "analyze_trace",
                "ingest", "tempo", "fetch", "shell", "execute",
            )
        ) or getattr(obs, "type", "") == "INPUT":
            tool_calls += 1
    print(f"## total observations (flat, incl. nested): {total}")
    print(f"## by type: {by_type}")
    print(f"## heuristic tool-call-ish count: {tool_calls}")
    print()
    print(f"{'#':>3} {'d':>2} {'type':<10} {'name':<40} {'start':>10} {'end':>10}")
    print("-" * 80)
    for i, (depth, obs) in enumerate(flat, 1):
        nm = (getattr(obs, "name", "") or "")[:40]
        t = getattr(obs, "type", "?") or "?"
        st = str(getattr(obs, "start_time", "") or "")[11:19]
        en = str(getattr(obs, "end_time", "") or "")[11:19]
        print(f"{i:>3} {depth:>2} {t:<10} {nm:<40} {st:>10} {en:>10}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id", nargs="?", default=None)
    ap.add_argument("--session", default=None)
    ap.add_argument("--bug", default=None)
    args = ap.parse_args()

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    trace = None
    if args.trace_id:
        resp = lf.fetch_trace(args.trace_id)
        trace = getattr(resp, "data", None)
        if trace is None:
            print(f"trace {args.trace_id} not found")
            return 1
    else:
        if not args.session or not args.bug:
            print("must give trace_id OR --session + --bug")
            return 2
        traces = _fetch_session_traces(lf, args.session)
        for t in traces:
            if _bug_id(t) == args.bug:
                trace = lf.fetch_trace(t.id).data
                break
        if trace is None:
            print(f"no trace for bug={args.bug} in session={args.session}")
            print("available:", [_bug_id(t) for t in traces])
            return 1

    obs = getattr(trace, "observations", None) or []
    flat = _walk_observations(obs)
    _print_obs(flat, trace.name, trace.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
