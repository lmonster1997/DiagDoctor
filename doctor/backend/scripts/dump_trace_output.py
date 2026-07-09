"""Fetch trace-level input/output for a single trace."""

import sys

from langfuse import Langfuse

from src.config import settings

lf = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

trace = lf.fetch_trace(sys.argv[1]).data
print("=== TRACE INPUT ===")
print(trace.input if trace.input else "<none>")
print("\n=== TRACE OUTPUT ===")
out = trace.output
if out is None:
    print("<none>")
elif isinstance(out, dict):
    import json

    print(json.dumps(out, ensure_ascii=False, indent=2)[:8000])
else:
    print(str(out)[:8000])
print("\n=== TRACE METADATA ===")
import json

print(json.dumps(trace.metadata or {}, ensure_ascii=False, indent=2)[:2000])
