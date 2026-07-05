"""Dump a single observation's input/output metadata for a trace.

Usage:
    uv run python scripts/dump_obs_detail.py <trace_id> [obs_name_substring ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402


def _walk(obs_list, out=None):
    if out is None:
        out = []
    for obs in obs_list:
        out.append(obs)
        children = getattr(obs, "children", None) or []
        if children:
            _walk(children, out)
    return out


def _short(v, n=2000):
    if v is None:
        return "<none>"
    if isinstance(v, (dict, list)):
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except Exception:
            s = str(v)
    else:
        s = str(v)
    if len(s) > n:
        s = s[:n] + f"\n... <truncated, total {len(s)} chars>"
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id")
    ap.add_argument("substrs", nargs="*", help="only print obs whose name contains one of these")
    ap.add_argument("--all", action="store_true", help="print all observations")
    args = ap.parse_args()

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )
    trace = lf.fetch_trace(args.trace_id).data
    flat = _walk(trace.observations or [])

    print(f"# Trace: {trace.name}  id={trace.id}\n# total flat: {len(flat)}\n")

    for i, obs in enumerate(flat, 1):
        nm = getattr(obs, "name", "") or ""
        if not args.all and args.substrs and not any(s in nm for s in args.substrs):
            continue
        t = getattr(obs, "type", "?") or "?"
        print(f"\n=== #{i}  type={t}  name={nm}  start={getattr(obs,'start_time',None)}  end={getattr(obs,'end_time',None)} ===")
        # input
        inp = getattr(obs, "input", None)
        print(f"-- input --\n{_short(inp, 3000)}")
        outp = getattr(obs, "output", None)
        print(f"-- output --\n{_short(outp, 3000)}")
        # metadata
        md = getattr(obs, "metadata", None)
        if md:
            print(f"-- metadata --\n{_short(md, 1000)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
