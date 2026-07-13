"""Diagnose why CONFIG-020's JSON fails to parse even with strict=False.

Fetches the raw last LLM output from Langfuse and tries multiple parsing
strategies, printing detailed error messages for each.
"""

from __future__ import annotations

import json
import os
import re
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else "baseline-15case-v1-20260706-154516"
    bug_id = sys.argv[2] if len(sys.argv) > 2 else "CONFIG-020"

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    # Fetch traces
    traces = []
    page = 1
    while True:
        resp = lf.fetch_traces(session_id=session_id, page=page, limit=100)
        batch = getattr(resp, "data", []) or []
        traces.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    # Find the target trace
    target = None
    for t in traces:
        md = t.metadata or {}
        if md.get("bug_id") == bug_id or bug_id in t.name:
            target = t
            break

    if not target:
        print(f"Trace not found for bug_id={bug_id}")
        return 1

    print(f"# Trace: {target.name}  (id={target.id})\n")

    # Fetch full trace with observations
    full = lf.fetch_trace(target.id).data
    obs = full.observations or []

    # Find LLM generation observations (sorted by start_time)
    llm_obs = [o for o in obs if o.type == "GENERATION"]
    llm_obs.sort(key=lambda o: o.start_time)

    if not llm_obs:
        print("No GENERATION observations found")
        return 1

    # Get last LLM output (same extraction logic as dump_trace_llm_responses.py)
    last_obs = llm_obs[-1]
    output = last_obs.output or {}
    content = ""
    if isinstance(output, dict):
        content = str(output.get("content", "") or "")
        if not content:
            gens = output.get("generations") or []
            if gens and isinstance(gens, list) and isinstance(gens[0], list):
                content = str(gens[0][0].get("text", "") or "")
    elif isinstance(output, str):
        content = output

    print(f"# Last LLM output length: {len(content)} chars\n")

    # Save raw content to file for inspection
    safe_bug = bug_id.replace("/", "_")
    out_file = f"debug_{safe_bug}_raw_output.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"# Raw output saved to: {out_file}\n")

    # Try parsing strategies
    print("=" * 60)
    print("# Parsing strategy tests:\n")

    # 1. Direct json.loads (strict=False)
    try:
        data = json.loads(content, strict=False)
        print(f"  [1] json.loads(strict=False): SUCCESS, keys={list(data.keys())}")
    except json.JSONDecodeError as e:
        print(f"  [1] json.loads(strict=False): FAIL - {e}")

    # 2. Direct json.loads (strict=True)
    try:
        data = json.loads(content)
        print(f"  [2] json.loads(strict=True): SUCCESS, keys={list(data.keys())}")
    except json.JSONDecodeError as e:
        print(f"  [2] json.loads(strict=True): FAIL - {e}")

    # 3. Extract from markdown fences
    fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(fence_pattern, content, re.DOTALL)
    print(f"  [3] Markdown fence matches: {len(matches)}")
    for i, m in enumerate(matches):
        try:
            data = json.loads(m, strict=False)
            print(f"      fence[{i}]: SUCCESS, keys={list(data.keys())}")
        except json.JSONDecodeError as e:
            print(f"      fence[{i}]: FAIL - {e}")

    # 4. Brace-depth extraction
    candidates = []
    depth = 0
    in_string = False
    escape_next = False
    start = -1
    for i, ch in enumerate(content):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append((start, i + 1))
                start = -1

    print(f"  [4] Brace-depth candidates: {len(candidates)}")
    for i, (s, e) in enumerate(reversed(candidates)):
        candidate = content[s:e]
        try:
            data = json.loads(candidate, strict=False)
            print(f"      candidate[{i}] (pos {s}-{e}): SUCCESS, keys={list(data.keys())}")
        except json.JSONDecodeError as ex:
            print(f"      candidate[{i}] (pos {s}-{e}): FAIL - {ex}")
            # Show context around error
            err_pos = ex.pos
            ctx_start = max(0, err_pos - 50)
            ctx_end = min(len(candidate), err_pos + 50)
            print(f"        error at pos {err_pos}, context:")
            print(f"        ...{repr(candidate[ctx_start:ctx_end])}...")

    # 5. Check for common issues
    print("\n# Content analysis:")
    print(f"  Starts with: {repr(content[:50])}")
    print(f"  Ends with: {repr(content[-50:])}")
    print(f"  Has literal newlines: {chr(10) in content}")
    print(f"  Has literal tabs: {chr(9) in content}")
    print(f"  Has literal CR: {chr(13) in content}")
    print(f"  Brace count: open={content.count('{')} close={content.count('}')}")

    # Check for literal control chars inside the content
    control_chars = {}
    for i, ch in enumerate(content):
        if ord(ch) < 32 and ch not in "\n\r\t":
            control_chars.setdefault(ord(ch), []).append(i)
    if control_chars:
        print(f"  Other control chars: { {hex(k): len(v) for k, v in control_chars.items()} }")
    else:
        print("  Other control chars: none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
