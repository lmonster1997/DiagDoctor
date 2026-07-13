"""Quick check: dump forced_final_json_call flag for each trace in a session."""

import sys

from langfuse import Langfuse

from src.config import settings

lf = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

sid = sys.argv[1]
traces = []
page = 1
while True:
    r = lf.fetch_traces(session_id=sid, page=page, limit=100)
    b = getattr(r, "data", []) or []
    traces.extend(b)
    if len(b) < 100:
        break
    page += 1

print(f"# Session: {sid}  ({len(traces)} traces)\n")
print(f"{'bug_id':<13}{'forced_call':>12}{'tool_calls':>12}{'early_stop':>12}")
print("-" * 49)

for t in sorted(traces, key=lambda x: str((x.metadata or {}).get("bug_id") or x.name)):
    md = t.metadata or {}
    bug = md.get("bug_id") or t.name.split("_")[-1]
    out = t.output if t.output else {}
    if isinstance(out, dict):
        forced = out.get("forced_final_json_call", "N/A")
        tc = out.get("tool_calls", "?")
        es = out.get("early_stopped", "?")
    else:
        forced = tc = es = "?"
    print(f"{bug:<13}{str(forced):>12}{str(tc):>12}{str(es):>12}")
