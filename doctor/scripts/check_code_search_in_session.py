"""Check whether code_search returned real results in the iter0 baseline."""

import sys

from langfuse import Langfuse

from src.config import settings

lf = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

session_id = sys.argv[1] if len(sys.argv) > 1 else "baseline-15case"
bug_filter = sys.argv[2] if len(sys.argv) > 2 else None

traces = []
page = 1
while True:
    resp = lf.fetch_traces(session_id=session_id, page=page, limit=100)
    batch = getattr(resp, "data", []) or []
    traces.extend(batch)
    if len(batch) < 100:
        break
    page += 1

print(f"# Session: {session_id}  ({len(traces)} traces)\n")


def _bug_id(t):
    md = t.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or t.name.split("_")[-1]


def _walk(obs_list, out=None):
    if out is None:
        out = []
    for obs in obs_list:
        out.append(obs)
        for c in getattr(obs, "children", None) or []:
            out.append(c)
    return out


for t in sorted(traces, key=lambda x: str(_bug_id(x))):
    bid = _bug_id(t)
    if bug_filter and bid != bug_filter:
        continue
    full = lf.fetch_trace(t.id).data
    flat = _walk(full.observations or [])
    cs_hits = 0
    cs_fallback = 0
    for obs in flat:
        nm = (getattr(obs, "name", "") or "").lower()
        if "code_search" not in nm:
            continue
        outp = getattr(obs, "output", None) or {}
        result = outp.get("result", "") if isinstance(outp, dict) else str(outp)
        if "fallback" in result or '"results": []' in result:
            cs_fallback += 1
        elif "file_path" in result or "match_type" in result:
            cs_hits += 1
    print(f"  {bid:<13} code_search hits={cs_hits}  fallback={cs_fallback}")
