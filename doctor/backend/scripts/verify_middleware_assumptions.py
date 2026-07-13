"""Verify 3 risk assumptions for the create_agent migration.

Run: cd doctor && uv run python scripts/verify_middleware_assumptions.py

Tests:
1. wrap_tool_call registration order: outer->inner or inner->outer?
   Plan depends on [dedup, langfuse, truncation] order = dedup outer (short-circuits skip).
2. after_agent returning {"messages": [AIMessage]} — does it show up in agent.ainvoke() result?
   ForcedFinalCallMiddleware depends on this.
3. config={"callbacks": [handler]} — does it propagate to internal LLM calls?
   Langfuse LLM generation observation depends on this.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

# ── Fake scripted chat model ─────────────────────────────────────────────


class ScriptedChatModel(BaseChatModel):
    """Returns scripted AIMessages in sequence. bind_tools returns self."""

    responses: list[AIMessage]
    _idx: int = PrivateAttr(default=0)

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        return self

    def _generate(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        resp = self.responses[self._idx]
        self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=resp)])

    async def _agenerate(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted"


# ── Fake tool ────────────────────────────────────────────────────────────


@tool
def echo_tool(text: str) -> str:
    """Echo the text back."""
    return f"echoed: {text}"


# ── Test 1: wrap_tool_call order ─────────────────────────────────────────

wrap_order: list[str] = []


class OuterWrap(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        wrap_order.append("outer-before")
        result = await handler(request)
        wrap_order.append("outer-after")
        return result


class InnerWrap(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        wrap_order.append("inner-before")
        result = await handler(request)
        wrap_order.append("inner-after")
        return result


async def test_wrap_order() -> None:
    wrap_order.clear()
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "tc1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(
        model,
        [echo_tool],
        middleware=[OuterWrap(), InnerWrap()],
    )
    await agent.ainvoke({"messages": [HumanMessage(content="run echo")], "recursion_limit": 10})
    print(f"[Test 1] wrap_tool_call execution order: {wrap_order}")
    if wrap_order == ["outer-before", "inner-before", "inner-after", "outer-after"]:
        print(
            "  => CONFIRMED: registration order = outer->inner (first registered wraps outermost)"
        )
    elif wrap_order == ["inner-before", "outer-before", "outer-after", "inner-after"]:
        print("  => CONFIRMED: registration order = inner->outer (last registered wraps outermost)")
    else:
        print("  => UNEXPECTED order — investigate")


# ── Test 2: after_agent append messages ──────────────────────────────────


after_agent_msg_count_before_append: list[int] = []


class AppendOnAfterAgent(AgentMiddleware):
    async def aafter_agent(self, state, runtime):  # type: ignore[no-untyped-def]
        msgs = state.get("messages", [])
        after_agent_msg_count_before_append.append(len(msgs))
        return {"messages": [AIMessage(content='{"FORCED_CALL_MARKER": true}')]}


async def test_after_agent_append() -> None:
    after_agent_msg_count_before_append.clear()
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="agent done without json"),
        ]
    )
    agent = create_agent(
        model,
        [echo_tool],
        middleware=[AppendOnAfterAgent()],
    )
    result = await agent.ainvoke({"messages": [HumanMessage(content="hi")], "recursion_limit": 10})
    final_msgs = result.get("messages", [])
    last_msg = final_msgs[-1] if final_msgs else None
    last_content = str(getattr(last_msg, "content", "")) if last_msg else ""
    print(
        f"[Test 2] messages count before after_agent append: {after_agent_msg_count_before_append}"
    )
    print(f"  final messages count: {len(final_msgs)}")
    print(f"  last message content: {last_content!r}")
    if "FORCED_CALL_MARKER" in last_content:
        print(
            "  => CONFIRMED: after_agent {'messages': [AIMessage]} IS appended"
            " and visible in ainvoke() result"
        )
    else:
        print(
            "  => FAILED: after_agent message append NOT visible in result — need fallback design"
        )


# ── Test 3: callbacks propagation ────────────────────────────────────────


class TrackingCallback(BaseCallbackHandler):
    def __init__(self) -> None:
        self.llm_starts = 0

    def on_llm_start(self, serialized, prompts, **kwargs):  # type: ignore[no-untyped-def]
        self.llm_starts += 1


async def test_callbacks_propagation() -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "tc1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_agent(model, [echo_tool], middleware=[])
    cb = TrackingCallback()
    await agent.ainvoke(
        {"messages": [HumanMessage(content="run echo")], "recursion_limit": 10},
        config={"callbacks": [cb]},
    )
    print(
        f"[Test 3] on_llm_start fired {cb.llm_starts} times (expect 2: one per scripted LLM call)"
    )
    if cb.llm_starts >= 2:
        print("  => CONFIRMED: config callbacks propagate to internal LLM calls")
    elif cb.llm_starts == 0:
        print("  => FAILED: callbacks do NOT propagate — need manual attach in wrap_model_call")
    else:
        print(f"  => PARTIAL: {cb.llm_starts} starts — may be partial propagation")


async def main() -> None:
    print("=" * 70)
    print("Verifying 3 risk assumptions for create_agent migration")
    print("=" * 70)
    print()
    await test_wrap_order()
    print()
    await test_after_agent_append()
    print()
    await test_callbacks_propagation()
    print()
    print("=" * 70)
    print("Done. Use these results to finalize middleware design.")


if __name__ == "__main__":
    asyncio.run(main())
