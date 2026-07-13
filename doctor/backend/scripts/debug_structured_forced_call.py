"""Diagnose whether with_structured_output worked on the forced final JSON call.

For each case in a session, pulls the trace and:
  1. Prints trace output_data (forced_final_json_call flag, report.notes, etc.)
  2. Lists every GENERATION observation with: name, content length, tool_calls
     (name + args preview) — so we can see if the LAST LLM call emitted a
     ForcedDiagnosisReport tool_call.
  3. For the last 2 GENERATION observations, dumps the full output dict as
     JSON so we can inspect exactly what came back (content / tool_calls /
     additional_kwargs).

Usage:
    uv run python scripts/debug_structured_forced_call.py <session_id> [bug_id]
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any

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


def _walk_observations(obs_list, depth, out):
    for obs in obs_list:
        out.append((depth, obs))
        children = getattr(obs, "children", None) or []
        if children:
            _walk_observations(children, depth + 1, out)
    return out


def _extract_tool_calls(output: Any) -> list[dict[str, Any]]:
    """Extract tool_calls from a langchain AIMessage-serialized output dict."""
    if not isinstance(output, dict):
        return []
    calls: list[dict[str, Any]] = []
    # Direct tool_calls field (langchain AIMessage.tool_calls)
    tc = output.get("tool_calls") or []
    if isinstance(tc, list):
        for c in tc:
            if isinstance(c, dict):
                calls.append(
                    {
                        "name": c.get("name", "?"),
                        "args": c.get("args", {}),
                        "id": c.get("id", ""),
                    }
                )
    # additional_kwargs.tool_calls (OpenAI raw format)
    if not calls:
        ak = output.get("additional_kwargs") or {}
        raw_tc = ak.get("tool_calls") if isinstance(ak, dict) else None
        if isinstance(raw_tc, list):
            for c in raw_tc:
                if isinstance(c, dict):
                    fn = c.get("function") or {}
                    args_str = fn.get("arguments", "")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except json.JSONDecodeError:
                        args = {"_raw": args_str}
                    calls.append(
                        {
                            "name": fn.get("name", "?"),
                            "args": args,
                            "id": c.get("id", ""),
                        }
                    )
    return calls


def _summarize_output(output: Any) -> dict[str, Any]:
    """Summarize a GENERATION observation's output for printing."""
    if not isinstance(output, dict):
        return {"type": type(output).__name__, "preview": str(output)[:200]}
    content = str(output.get("content", "") or "")
    tool_calls = _extract_tool_calls(output)
    return {
        "content_len": len(content),
        "content_preview": content[:300],
        "tool_calls": tool_calls,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: debug_structured_forced_call.py <session_id> [bug_id]")
        return 1
    session_id = sys.argv[1]
    only_bug = sys.argv[2] if len(sys.argv) > 2 else None

    # Reconfigure stdout to utf-8 so emoji / Chinese don't blow up on gbk cp.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    lf = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    traces = _fetch_session_traces(lf, session_id)
    print(f"# Session: {session_id}  ({len(traces)} traces)\n")

    for t in traces:
        bid = _bug_id(t)
        if only_bug and bid != only_bug:
            continue
        full = lf.fetch_trace(t.id).data
        print("=" * 70)
        print(f"# Trace: {full.name}  (bug_id={bid})  id={full.id}")

        # ── 1. trace output_data (forced_final_json_call flag etc.) ──
        out_data = getattr(full, "output", None) or {}
        if isinstance(out_data, dict):
            print("\n## trace.output_data:")
            for k in ("forced_final_json_call", "early_stopped", "tool_calls"):
                if k in out_data:
                    print(f"    {k} = {out_data.get(k)!r}")
            report = out_data.get("diagnosis_report") or {}
            if isinstance(report, dict):
                print(f"    report.primary_category = {report.get('primary_category')!r}")
                print(f"    report.confidence = {report.get('confidence')!r}")
                print(f"    report.notes = {report.get('notes')!r}")

        # ── 2. GENERATION observations ──
        obs = getattr(full, "observations", None) or []
        flat = _walk_observations(obs, 0, [])
        gens = [(d, o) for d, o in flat if (getattr(o, "type", "?") or "?") == "GENERATION"]
        gens.sort(key=lambda x: getattr(x[1], "start_time", None))
        print(f"\n## GENERATION count: {len(gens)}")

        for i, (_depth, o) in enumerate(gens, 1):
            output = getattr(o, "output", None) or {}
            summary = _summarize_output(output)
            is_last = i == len(gens)
            tag = " [LAST = forced call]" if is_last else ""
            print(f"\n  --- LLM call #{i}{tag}  (name={getattr(o, 'name', '')}) ---")
            print(f"    content_len: {summary['content_len']}")
            if summary["content_preview"]:
                print(f"    content_preview: {summary['content_preview']!r}")
            tcs = summary["tool_calls"]
            if tcs:
                print(f"    tool_calls ({len(tcs)}):")
                for c in tcs:
                    args_preview = json.dumps(c["args"], ensure_ascii=False)
                    if len(args_preview) > 400:
                        args_preview = args_preview[:400] + "... [truncated]"
                    print(f"      name={c['name']!r}  id={c['id']!r}")
                    print(f"      args={args_preview}")
            else:
                print("    tool_calls: (none)")

        # ── 3. Full input + output dump for last 2 GENERATIONs ──
        print("\n## Full input + output dump (last 2 GENERATIONs):")
        for i, (_depth, o) in enumerate(gens[-2:], len(gens) - 1):
            output = getattr(o, "output", None) or {}
            inp = getattr(o, "input", None) or {}
            print(f"\n  === LLM call #{i} full INPUT ===")
            try:
                inp_str = json.dumps(inp, ensure_ascii=False, indent=2, default=str)
                print(inp_str[:2500])
            except Exception as e:
                print(f"  (failed to serialize input: {e})")
                print(str(inp)[:2000])
            print(f"\n  === LLM call #{i} full OUTPUT ===")
            try:
                print(json.dumps(output, ensure_ascii=False, indent=2, default=str)[:3000])
            except Exception as e:
                print(f"  (failed to serialize: {e})")
                print(str(output)[:2000])

        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
