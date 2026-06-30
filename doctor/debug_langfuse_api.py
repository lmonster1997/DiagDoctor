"""Debug script: explore langfuse v3 API."""

import inspect

from langfuse import Langfuse

from src.config import settings

client = Langfuse(
    secret_key=settings.langfuse_secret_key,
    public_key=settings.langfuse_public_key,
    host=settings.langfuse_host,
)

# Check API signatures
for method_name in ["start_observation", "start_span", "start_generation"]:
    method = getattr(client, method_name)
    sig = inspect.signature(method)
    print(f"\n{method_name} params:")
    for name, param in sig.parameters.items():
        default = param.default if param.default is not inspect.Parameter.empty else "required"
        print(f"  {name}: {default}")

# Test creating a trace with correct v3 API
print("\n\n=== Testing v3 API ===")
trace_id = client.create_trace_id()
print(f"Trace ID: {trace_id}")

# Try start_observation
obs = client.start_observation(
    trace_id=trace_id,
    name="test-trace-v3",
    input={"test": True},
    as_type="span",
)
print(f"Observation type: {type(obs).__name__}")

# Create a generation nested under it
gen = client.start_generation(
    trace_id=trace_id,
    parent_observation_id=obs.id,
    name="test-gen",
    model="test-model",
    input={"messages": [{"role": "user", "content": "hello"}]},
)
print(f"Generation type: {type(gen).__name__}")

gen.update(output={"content": "world"}, usage={"input": 10, "output": 5})
gen.end()

obs.update(output={"result": "ok"})
obs.end()

client.flush()
print("Flushed! Check Langfuse Dashboard.")
