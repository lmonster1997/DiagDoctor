"""Debug: Explore langfuse v3 API for trace creation."""

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

# Check TraceContext type
print("=== TraceContext ===")

if hasattr(TraceContext, "__annotations__"):
    print(f"Annotations: {TraceContext.__annotations__}")

# Try creating with trace_context
trace_id = client.create_trace_id()
print(f"\nTrace ID: {trace_id}")

# Approach 1: Use trace_context as dict
tc = TraceContext(trace_id=trace_id)
print(f"TraceContext: {tc}")

obs = client.start_observation(
    trace_context=tc,
    name="test-trace-v3",
    input={"test": True},
)
print(f"Observation created: {type(obs).__name__}, id={obs.id}")

# Create generation under it
gen = client.start_generation(
    trace_context=tc,
    parent_observation_id=obs.id,
    name="test-gen",
    model="test-model",
    input={"messages": [{"role": "user", "content": "hello"}]},
)
print(f"Generation created: {type(gen).__name__}")

gen.update(output={"content": "world"}, usage_details={"input": 10, "output": 5})
gen.end()

obs.update(output={"result": "ok"})
obs.end()

client.flush()
print("Flushed! Check Langfuse Dashboard at http://127.0.0.1:3002")
