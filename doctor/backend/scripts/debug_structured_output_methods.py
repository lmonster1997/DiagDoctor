"""Standalone probe: does DeepSeek accept `with_structured_output(method="json_schema")`?

Background: `forced_call.py` currently uses `method="function_calling"` because
an earlier test hit a DeepSeek 400 — `'This response_format type is unavailable now'`.
The hypothesis is that the 400 was actually caused by the doctor process not
being restarted after a code change, not by DeepSeek actually rejecting
`json_schema`. This script tests the hypothesis directly without touching
forced_call.py or running a full diagnosis.

Tries three methods in sequence and reports which succeed / fail:
  - method="json_schema"  (the default — OpenAI Structured Output API)
  - method="json_mode"    (OpenAI JSON mode)
  - method="function_calling"  (current setting in forced_call.py — baseline)

Each attempt uses a tiny prompt + the ForcedDiagnosisReport schema, with
include_raw=True so we can see the raw response / error.

Usage:
    uv run python scripts/debug_structured_output_methods.py
"""
from __future__ import annotations

import asyncio
import sys

from langchain_core.messages import HumanMessage, SystemMessage

sys.path.insert(0, ".")
from src.graph.nodes.diagnosis_agent import ForcedDiagnosisReport
from src.llm_factory import get_llm_for_role

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


PROMPT_MESSAGES = [
    SystemMessage(content="你是诊断助手。基于证据输出结构化诊断报告。"),
    HumanMessage(
        content=(
            "证据：backend/app/api/tasks.py 第 37 行 .order_by(Task.created_at.asc()) "
            "导致任务列表升序，与用户期望相反。"
            "请输出 ForcedDiagnosisReport。"
        )
    ),
]


async def try_method(llm, method: str) -> dict:
    """Try one with_structured_output method. Returns a result dict for printing."""
    out: dict = {"method": method, "ok": False, "error": None, "parsed": None, "raw_content_len": 0}
    try:
        structured_llm = llm.with_structured_output(
            ForcedDiagnosisReport, method=method, include_raw=True
        )
        result = await asyncio.wait_for(
            structured_llm.ainvoke(PROMPT_MESSAGES),
            timeout=60,
        )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    if not isinstance(result, dict):
        out["error"] = f"unexpected result type: {type(result).__name__}"
        return out

    parsed = result.get("parsed")
    raw = result.get("raw")
    parsing_error = result.get("parsing_error")

    if parsing_error is not None:
        out["error"] = f"parsing_error: {parsing_error!r}"

    if parsed is not None:
        out["ok"] = True
        out["parsed"] = {
            "primary_category": parsed.primary_category,
            "confidence": parsed.confidence,
            "affected_file": parsed.affected_file,
            "root_cause_preview": str(parsed.root_cause)[:200],
        }

    if raw is not None:
        out["raw_content_len"] = len(str(getattr(raw, "content", "") or ""))
        # Also surface tool_calls / additional_kwargs for diagnostic visibility
        tc = getattr(raw, "tool_calls", None)
        ak = getattr(raw, "additional_kwargs", None)
        out["raw_tool_calls_count"] = len(tc) if isinstance(tc, list) else 0
        if ak:
            # response_format metadata sometimes lands here
            out["raw_additional_kwargs_keys"] = list(ak.keys()) if isinstance(ak, dict) else type(ak).__name__

    return out


async def main() -> int:
    llm = get_llm_for_role("diagnosis")
    model_name = getattr(llm, "model_name", getattr(llm, "model", "?"))
    base_url = getattr(llm, "base_url", "?")
    print(f"# Probe: with_structured_output methods on diagnosis LLM")
    print(f"# model={model_name!r}  base_url={base_url!r}")
    print()

    methods = ["json_schema", "json_mode", "function_calling"]
    for method in methods:
        print(f"--- method={method!r} ---")
        # NOTE: get_llm_for_role is lru_cached, so all 3 attempts share the
        # same LLM instance. with_structured_output returns a derived
        # Runnable — it does NOT mutate the underlying llm — so each method
        # gets a fresh derivation. Safe to reuse the same llm.
        result = await try_method(llm, method)
        if result["ok"]:
            print(f"  OK ✓  parsed.primary_category={result['parsed']['primary_category']!r}")
            print(f"        parsed.confidence={result['parsed']['confidence']}")
            print(f"        parsed.affected_file={result['parsed']['affected_file']!r}")
            print(f"        raw_content_len={result['raw_content_len']}  raw_tool_calls_count={result.get('raw_tool_calls_count', 0)}")
        else:
            print(f"  FAIL ✗  error={result['error']}")
            print(f"        raw_content_len={result['raw_content_len']}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
