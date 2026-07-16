"""Dump tool-call observations for a Langfuse trace - focus on truncation.

Usage: cd doctor/backend && uv run python scripts/dump_trace_truncation.py <trace_id>
"""

from __future__ import annotations

import json
import sys

import requests

from src.config import settings

HOST = settings.langfuse_host.rstrip("/")
AUTH = (settings.langfuse_public_key, settings.langfuse_secret_key)


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{HOST}/api/public{path}", params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def _len(x: object) -> int:
    if x is None:
        return 0
    return len(json.dumps(x, ensure_ascii=False))


def main() -> None:
    tid = sys.argv[1]
    obs: list[dict] = []
    page = 1
    while True:
        d = _get("/observations", {"traceId": tid, "limit": 100, "page": page}).get("data", [])
        obs += d
        if len(d) < 100:
            break
        page += 1
        if page > 10:
            break

    print(f"trace {tid}: {len(obs)} observations\n")
    # sort by start_time to get execution order
    obs.sort(key=lambda o: o.get("startTime") or o.get("start_time") or "")

    for i, o in enumerate(obs):
        otype = o.get("type", "")
        name = (o.get("name") or "")[:60]
        inp = o.get("input")
        out = o.get("output")
        meta = o.get("metadata") or {}
        print(f"[{i:02d}] {otype:<10} | {name:<50} | in={_len(inp):>6} out={_len(out):>6}")

    # Dump tool-call outputs in detail (search_observability especially)
    print("\n" + "=" * 80)
    print("TOOL OUTPUTS (full, to inspect truncation)")
    print("=" * 80)
    for i, o in enumerate(obs):
        meta = o.get("metadata") or {}
        inp = o.get("input")
        name = o.get("name") or ""
        is_tool = (
            bool(meta.get("tool_name"))
            or "tool" in name.lower()
            or (isinstance(inp, dict) and "args" in inp)
        )
        if not is_tool:
            continue
        out = o.get("output")
        print(f"\n--- obs[{i}] {name} tool={meta.get('tool_name')} out_len={_len(out)} ---")
        print("INPUT:", json.dumps(inp, ensure_ascii=False)[:500])
        out_str = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        print("OUTPUT (first 1500 + last 400 chars):")
        if len(out_str) > 1900:
            print(out_str[:1500])
            print(f"   ...[+{len(out_str) - 1900} chars]...")
            print(out_str[-400:])
        else:
            print(out_str)


if __name__ == "__main__":
    main()
