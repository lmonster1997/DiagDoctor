"""Quick check: for a session, dump each trace's early_stopped + JSON parse status."""
import sys
from langfuse import Langfuse
from src.config import settings
from src.graph.nodes.diagnosis_agent import _extract_json_from_text

lf = Langfuse(secret_key=settings.langfuse_secret_key, public_key=settings.langfuse_public_key, host=settings.langfuse_host)

session_id = sys.argv[1]
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
print(f"{'bug_id':<13}{'tool_calls':>11}{'early_stop':>11}{'json_ok':>8}{'root':>7}{'cat':>6}{'file':>6}{'fix':>6}")
print("-" * 70)

def _bug_id(t):
    md = t.metadata or {}
    return md.get("bug_id") or md.get("recipe_id") or t.name.split("_")[-1]

def _latest_scores(t):
    full = lf.fetch_trace(t.id).data
    latest = {}
    for s in (full.scores or []):
        cur = latest.get(s.name)
        ts_s = getattr(s, "timestamp", None)
        ts_c = getattr(cur, "timestamp", None) if cur else None
        if cur is None or (ts_s and ts_c and ts_s > ts_c):
            latest[s.name] = s
    return {n: getattr(s, "value", None) for n, s in latest.items()}

for t in sorted(traces, key=lambda x: str(_bug_id(x))):
    out = t.output if t.output else {}
    if isinstance(out, dict):
        tc = out.get("tool_calls", "?")
        es = out.get("early_stopped", "?")
        rep = out.get("diagnosis_report") or {}
        notes = rep.get("notes", "") or ""
        primary = rep.get("primary_category", "") or ""
        # json_ok: True if the report has a non-empty primary_category AND
        # notes doesn't indicate a parse failure. (Earlier version tried to
        # _extract_json_from_text on root_cause, which is a plain-text root
        # cause string — that always returned None and false-reported no.)
        json_ok = "yes" if (primary and "JSON" not in notes) else "no"
    else:
        tc = es = json_ok = "?"
        notes = str(out)[:60]
    sc = _latest_scores(t)
    print(f"{_bug_id(t):<13}{str(tc):>11}{str(es):>11}{json_ok:>8}"
          f"{sc.get('root_cause_accuracy','-')!s:>7}{sc.get('category_accuracy','-')!s:>6}"
          f"{sc.get('affected_file_accuracy','-')!s:>6}{sc.get('fix_suggestion_quality','-')!s:>6}")
    if notes and "JSON" in notes:
        print(f"    notes: {notes[:80]}")
