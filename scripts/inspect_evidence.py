"""Quick inspector for FE-020 evidence files."""

import json
from pathlib import Path

base = Path("bug-factory/output/FE-020/evidence")

# ── browser_errors ──
be = json.loads((base / "browser_errors.json").read_text(encoding="utf-8"))
print("=== browser_errors.json ===")
for i, e in enumerate(be):
    msg = e.get("message", "")
    cs = str(e.get("component_stack", ""))
    print(f"[{i}] trace_id={e.get('trace_id')} span_id={e.get('span_id')}")
    print(f"    component_stack is None: {e.get('component_stack') is None}")
    print(f"    stack is None: {e.get('stack') is None}")
    print(f"    TaskBoard in msg: {'TaskBoard' in msg}")
    print(f"    Cannot read undefined: {'Cannot read properties of undefined' in msg}")

# ── logs ──
logs = json.loads((base / "logs.json").read_text(encoding="utf-8"))
print(f"\n=== logs.json ({len(logs)} entries) ===")
print(f"first log keys: {list(logs[0].keys())}")
for i, log in enumerate(logs[:3]):
    labels = log.get("labels", {})
    svc = labels.get("service_name", labels.get("service", ""))
    lvl = labels.get("detected_level", log.get("level", ""))
    tid = labels.get("trace_id", log.get("trace_id", ""))
    line = log.get("line", "")
    msg = log.get("message", "")
    content = line or msg
    print(f"[{i}] labels_keys={list(labels.keys())[:8]}")
    print(f"    svc={svc!r} lvl={lvl!r} tid={tid!r}")
    print(f"    content={str(content)[:200]}")

print("\nAll logs with service_name or content:")
for i, log in enumerate(logs):
    labels = log.get("labels", {})
    svc = labels.get("service_name", labels.get("service", ""))
    lvl = labels.get("detected_level", log.get("level", ""))
    tid = labels.get("trace_id", log.get("trace_id", ""))
    line = log.get("line", "")
    msg = log.get("message", "")
    content = line or msg
    if svc or lvl or tid or content:
        sc = str(content)[:120]
        print(f"  [{i:2d}] svc={str(svc):20s} lvl={str(lvl):8s} tid={str(tid)} content={sc}")

# ── traces ──
traces = json.loads((base / "traces.json").read_text(encoding="utf-8"))
print(f"\n=== traces.json ({len(traces)} spans) ===")
print(f"first span keys: {list(traces[0].keys())}")
has_name = sum(1 for s in traces if s.get("name"))
has_op = sum(1 for s in traces if s.get("operation_name"))
print(f"spans with name={has_name}, with operation_name={has_op}")

fe = [s for s in traces if "frontend" in str(s.get("service_name", "")).lower()]
be_spans = [s for s in traces if "backend" in str(s.get("service_name", "")).lower()]
print(f"\nfrontend: {len(fe)}, backend: {len(be_spans)}")

# operation_name dist
ops = {}
for s in traces:
    op = s.get("operation_name", s.get("name", ""))
    ops[op] = ops.get(op, 0) + 1
print(f"\noperation_name distribution ({len(ops)} unique):")
for op, cnt in sorted(ops.items(), key=lambda x: -x[1])[:15]:
    print(f"  {cnt:4d}x {op[:100]}")

# Trace_id overlap
span_tids = {s.get("trace_id") for s in traces}
be_tids = {e.get("trace_id") for e in be}
print(f"\nspan trace_ids: {len(span_tids)} unique")
print(f"browser trace_ids: {be_tids}")
print(f"OVERLAP: {span_tids & be_tids}")
