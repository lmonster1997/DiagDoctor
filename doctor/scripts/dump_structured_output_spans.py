"""Dump all SPAN observations named `structured_output_*` for a session's traces.

Verifies the Iteration 2 observability fix: `record_structured_output` should
create a SPAN observation on each trace where forced_call=True, with the parsed
Pydantic object visible in `output.parsed`.

Usage:
    uv run python scripts/dump_structured_output_spans.py <session_id> [bug_id]
"""

from __future__ import annotations

import json
import os
import sys

from langfuse import Langfuse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _bug_id(trace) -> str:
    md = trace.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or trace.name.split("_")[-1]


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: dump_structured_output_spans.py <session_id> [bug_id]")
        return 1
    session_id = sys.argv[1]
    only_bug = sys.argv[2] if len(sys.argv) > 2 else None

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    traces = []
    page = 1
    while True:
        r = lf.fetch_traces(session_id=session_id, page=page, limit=100)
        b = getattr(r, "data", []) or []
        traces.extend(b)
        if len(b) < 100:
            break
        page += 1

    print(f"# Session: {session_id}  ({len(traces)} traces)\n")

    for t in traces:
        bid = _bug_id(t)
        if only_bug and bid != only_bug:
            continue
        full = lf.fetch_trace(t.id).data
        print("=" * 70)
        print(f"# Trace: {full.name}  (bug_id={bid})")

        obs = getattr(full, "observations", None) or []
        flat = []

        def walk(o, d=0):
            for x in o:
                flat.append((d, x))
                walk(getattr(x, "children", None) or [], d + 1)

        walk(obs)

        # List all SPAN observations (focus on structured_output_*)
        spans = [(d, o) for d, o in flat if (getattr(o, "type", "?") or "?") == "SPAN"]
        structured = [
            (d, o) for d, o in spans if "structured_output" in (getattr(o, "name", "") or "")
        ]

        print(f"  total SPAN observations: {len(spans)}")
        print(f"  structured_output SPANs: {len(structured)}")

        for d, o in structured:
            nm = getattr(o, "name", "") or ""
            outp = getattr(o, "output", None) or {}
            meta = getattr(o, "metadata", None) or {}
            print(f"\n  --- SPAN name={nm!r} ---")
            if isinstance(outp, dict):
                keys = list(outp.keys())
                print(f"    output keys: {keys}")
                parsed = outp.get("parsed")
                if isinstance(parsed, dict):
                    print(f"    parsed.primary_category = {parsed.get('primary_category')!r}")
                    print(f"    parsed.confidence = {parsed.get('confidence')!r}")
                    print(f"    parsed.affected_file = {parsed.get('affected_file')!r}")
                    print(
                        f"    parsed.root_cause (first 200 chars) = {str(parsed.get('root_cause', ''))[:200]!r}"
                    )
                elif parsed is None:
                    print("    parsed = None  (failure path)")
                else:
                    print(f"    parsed type = {type(parsed).__name__}")
                rc = outp.get("raw_content")
                if rc is not None:
                    print(f"    raw_content_len = {len(str(rc))}")
                rtc = outp.get("raw_tool_calls")
                if rtc is not None:
                    print(
                        f"    raw_tool_calls count = {len(rtc) if isinstance(rtc, list) else '?'}"
                    )
            else:
                print(f"    output type: {type(outp).__name__}")
            print(f"    metadata: {json.dumps(meta, ensure_ascii=False)}")

        # Also list all SPAN names briefly for context
        print(f"\n  all SPAN names: {[getattr(o, 'name', '') for _, o in spans]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
