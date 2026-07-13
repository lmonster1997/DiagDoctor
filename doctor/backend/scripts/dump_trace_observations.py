"""Dump observations for a trace (by recipe_id within a session)."""

from __future__ import annotations

import sys

import requests

from src.config import settings

HOST = settings.langfuse_host.rstrip("/")
AUTH = (settings.langfuse_public_key, settings.langfuse_secret_key)


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{HOST}/api/public{path}"
    if params:
        from urllib.parse import urlencode

        url += "?" + urlencode(params)
    r = requests.get(url, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    session_id = sys.argv[1]
    recipe_id = sys.argv[2]
    # find trace
    data = _get("/traces", {"sessionId": session_id, "limit": 100})
    traces = data.get("data", [])
    target = None
    for t in traces:
        meta = t.get("metadata") or {}
        if meta.get("recipe_id") == recipe_id or recipe_id in (t.get("name") or ""):
            target = t
            break
    if not target:
        print(f"trace for {recipe_id} not found in {session_id}")
        sys.exit(1)
    trace_id = target["id"]
    print(f"trace: {target.get('name')}  id={trace_id}")
    # fetch observations for this trace
    obs_data = _get("/observations", {"traceId": trace_id, "limit": 100})
    obs = obs_data.get("data", [])
    print(f"  {len(obs)} observations:")
    for o in obs:
        name = o.get("name", "")
        otype = o.get("type", "")
        inp = o.get("input")
        args_key = ""
        if isinstance(inp, dict):
            args_key = str(inp.get("args") or inp.get("tool_call") or "")[:80]
        print(f"    [{otype:<10}] {name:<40} args={args_key}")


if __name__ == "__main__":
    main()
