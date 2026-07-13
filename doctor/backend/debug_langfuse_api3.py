"""Debug: Explore langfuse v3 nesting API."""

import sys

sys.path.insert(0, "src")

from langfuse import Langfuse
from langfuse.types import TraceContext

from src.config import settings

client = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

# Explore nesting
trace_id = client.create_trace_id()
print(f"Trace ID: {trace_id}")

tc = TraceContext(trace_id=trace_id)

# Approach: use parent_span_id in trace_context for nesting
obs = client.start_observation(
    trace_context=tc,
    name="test-trace-v3",
    input={"test": True},
)
print(f"Span: id={obs.id}")

# Try nesting with trace_context that includes parent_span_id
tc2 = TraceContext(trace_id=trace_id, parent_span_id=obs.id)
gen = client.start_generation(
    trace_context=tc2,
    name="test-gen",
    model="test-model",
    input={"messages": [{"role": "user", "content": "hello"}]},
)
print(f"Generation: id={gen.id}")

gen.update(output={"content": "world"}, usage_details={"input": 10, "output": 5})
gen.end()

# Also test start_span for tool calls
tc3 = TraceContext(trace_id=trace_id, parent_span_id=obs.id)
tool_span = client.start_span(
    trace_context=tc3,
    name="test-tool",
    input={"tool_name": "search", "args": {"query": "test"}},
)
print(f"Tool span: id={tool_span.id}")
tool_span.update(output={"result": "found"})
tool_span.end()

obs.update(output={"result": "ok"})
obs.end()

client.flush()
print("Flushed! Check Langfuse Dashboard: http://127.0.0.1:3002")
