"""Minimal isolated test: does DeepSeek support with_structured_output?

Bypasses the whole pipeline — just constructs a fake diagnosis conversation
and calls llm.with_structured_output(ForcedDiagnosisReport, include_raw=True)
directly. Prints exactly what comes back (or the exception).

Usage:
    uv run python scripts/debug_structured_output_minimal.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.engine.forced_call import ForcedDiagnosisReport
from src.llm_factory import get_llm_for_role


async def main() -> int:
    # Reconfigure stdout to utf-8 for Windows gbk cp.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    llm = get_llm_for_role("diagnosis")
    print(f"# LLM type: {type(llm).__name__}")
    print(f"# model: {getattr(llm, 'model_name', None) or getattr(llm, 'model', None)}")

    # Simulate a short diagnosis conversation ending in a narrative conclusion.
    # We don't include any tool_call history to keep this minimal — the goal
    # is just to see whether with_structured_output works AT ALL on DeepSeek.
    messages = [
        SystemMessage(content="你是 DiagDoctor 诊断引擎。"),
        HumanMessage(content="用户报告：登录成功后马上掉登录态。请诊断根因。"),
        AIMessage(
            content=(
                "根因是 config.py 第 32 行 jwt_expire_minutes: int = 0 覆盖了第 28 行的 60，"
                '导致 token 签发后立即过期，表现为"登录成功后立即掉登录态"。'
            )
        ),
        HumanMessage(
            content=(
                "你已达到工具调用上限，无法再调用任何工具。\n"
                "请基于上方对话历史中已收集到的所有证据，立即输出最终诊断报告 JSON。\n"
                "不要解释、不要重复证据、不要试图调用工具——只输出一个完整的 JSON 对象。\n"
                "现在输出 JSON："
            )
        ),
    ]

    print("\n# Test 1: with_structured_output(ForcedDiagnosisReport, include_raw=True)")
    structured_llm = llm.with_structured_output(ForcedDiagnosisReport, include_raw=True)
    try:
        result = await asyncio.wait_for(structured_llm.ainvoke(messages), timeout=60)
        print(f"  returned type: {type(result).__name__}")
        if isinstance(result, dict):
            parsed = result.get("parsed")
            raw = result.get("raw")
            print(f"  parsed: {parsed!r}")
            if parsed is not None:
                print(f"  parsed.primary_category: {parsed.primary_category!r}")
                print(f"  parsed.confidence: {parsed.confidence!r}")
                print(f"  parsed.model_dump_json: {parsed.model_dump_json(indent=2)}")
            print(f"  raw type: {type(raw).__name__}")
            print(f"  raw.content (first 300): {str(getattr(raw, 'content', ''))[:300]!r}")
            print(f"  raw.tool_calls: {getattr(raw, 'tool_calls', None)!r}")
            print(f"  raw.additional_kwargs: {getattr(raw, 'additional_kwargs', None)!r}")
        else:
            print(f"  (not a dict) repr: {result!r}")
    except Exception as exc:
        print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
        print("  traceback:")
        traceback.print_exc()

    print("\n# Test 3: with_structured_output(method='function_calling', include_raw=True)")
    structured_llm3 = llm.with_structured_output(
        ForcedDiagnosisReport, method="function_calling", include_raw=True
    )
    try:
        result3 = await asyncio.wait_for(structured_llm3.ainvoke(messages), timeout=60)
        print(f"  returned type: {type(result3).__name__}")
        if isinstance(result3, dict):
            parsed = result3.get("parsed")
            raw = result3.get("raw")
            print(f"  parsed: {parsed!r}")
            if parsed is not None:
                print(f"  parsed.primary_category: {parsed.primary_category!r}")
                print(f"  parsed.confidence: {parsed.confidence!r}")
                print(f"  parsed.model_dump_json:\n{parsed.model_dump_json(indent=2)}")
            print(f"  raw type: {type(raw).__name__}")
            print(f"  raw.content (first 300): {str(getattr(raw, 'content', ''))[:300]!r}")
            print(f"  raw.tool_calls: {getattr(raw, 'tool_calls', None)!r}")
        else:
            print(f"  (not a dict) repr: {result3!r}")
    except Exception as exc:
        print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    print("\n# Test 4: with_structured_output(method='function_calling') [no include_raw]")
    structured_llm4 = llm.with_structured_output(ForcedDiagnosisReport, method="function_calling")
    try:
        result4 = await asyncio.wait_for(structured_llm4.ainvoke(messages), timeout=60)
        print(f"  returned type: {type(result4).__name__}")
        print(f"  primary_category: {result4.primary_category!r}")
        print(f"  confidence: {result4.confidence!r}")
        print(f"  model_dump_json:\n{result4.model_dump_json(indent=2)}")
    except Exception as exc:
        print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
