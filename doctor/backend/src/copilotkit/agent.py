"""CopilotKit Agent — bridges CopilotKit SDK ↔ DiagDoctor LangGraph."""

from __future__ import annotations

import uuid as _uuid

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder

from copilotkit import LangGraphAGUIAgent


class DiagDoctorAgent(LangGraphAGUIAgent):
    """LangGraphAGUIAgent subclass with execute() bridge + smart resume."""

    async def execute(  # type: ignore[override]
        self,
        *,
        state: dict,
        config: dict | None = None,
        messages: list,
        thread_id: str,
        actions: list | None = None,
        meta_events: list | None = None,
        **kwargs,
    ):
        import asyncio

        node_name = kwargs.get("node_name")
        forwarded_props: dict = {}
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

    async def get_state(self, *, thread_id: str):  # type: ignore[override]
        """Smart resume: fresh if completed, resume if interrupted."""
        try:
            state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
            values = state.values or {}
            has_report = bool(values.get("report"))
            if has_report:
                return {"threadId": thread_id, "threadExists": False, "state": {}}
            return {
                "threadId": thread_id,
                "threadExists": bool(values),
                "state": values,
            }
        except Exception:
            return {"threadId": thread_id, "threadExists": False, "state": {}}
