"""CopilotKit Agent — bridges CopilotKit SDK ↔ DiagDoctor LangGraph."""

from __future__ import annotations

import uuid as _uuid
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder

from copilotkit import LangGraphAGUIAgent


class DiagDoctorAgent(LangGraphAGUIAgent):
    """LangGraphAGUIAgent subclass with execute() bridge + smart resume."""

    async def execute(
        self,
        *,
        state: dict[str, Any],
        config: dict[str, Any] | None = None,
        messages: list[Any],
        thread_id: str,
        actions: list[Any] | None = None,
        meta_events: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        import asyncio

        node_name = kwargs.get("node_name")
        forwarded_props: dict[str, Any] = {}
        if node_name:
            forwarded_props["node_name"] = node_name

        run_input = RunAgentInput(
            thread_id=thread_id,
            run_id=str(_uuid.uuid4()),
            messages=messages,
            state=state,
            tools=[],
            context=[],
            forwarded_props=forwarded_props,
        )

        encoder = EventEncoder()
        async for event in self.run(run_input):
            yield encoder.encode(event).encode("utf-8")
            await asyncio.sleep(0)

    async def get_state(self, *, thread_id: str) -> dict[str, Any]:
        """Smart resume: resume if paused/interrupted or no report; fresh if completed.

        #5 HITL: a diagnosis paused at the ``human_input`` interrupt has
        ``state.next`` non-empty (the graph is mid-execution). CopilotKit must
        resume from that checkpoint, not start fresh -- otherwise the paused
        HITL state (prior findings, the early_stopped report, the pending
        guidance request) is lost and the operator can never steer the run.
        """
        try:
            state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
            values = state.values or {}
            # Paused mid-graph (e.g. at the human_input interrupt) -> resume.
            if state.next:
                return {"threadId": thread_id, "threadExists": True, "state": values}
            # Completed run with a report -> fresh start.
            if bool(values.get("report")):
                return {"threadId": thread_id, "threadExists": False, "state": {}}
            # No report and not paused -> resume whatever partial state exists.
            return {
                "threadId": thread_id,
                "threadExists": bool(values),
                "state": values,
            }
        except Exception:
            return {"threadId": thread_id, "threadExists": False, "state": {}}
