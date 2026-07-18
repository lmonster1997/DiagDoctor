"""Custom /api/copilotkit/agent/default/run endpoint that forwards forwardedProps.

Why this exists (#5 F1 resume bug):
  CopilotKit 1.62.x backend (``copilotkit/integrations/fastapi.py`` handler for
  ``/agent/{name}``) extracts only ``threadId/state/messages/actions/nodeName``
  from the run request body and DROPS ``forwardedProps``. The downstream chain
  (``handle_execute_agent`` -> ``sdk.execute_agent`` -> ``agent.execute``) never
  receives it, so the ``RunAgentInput`` handed to ``ag_ui_langgraph`` has an
  EMPTY ``forwarded_props``. ``ag_ui_langgraph``'s ``prepare_stream`` reads
  ``forwarded_props.command.resume`` to resume an interrupt -> with it empty,
  the resume never fires (no ``human_input_resumed``, card stuck).

This endpoint reconstructs ``RunAgentInput`` WITH ``forwarded_props`` taken from
the body's ``forwardedProps`` and calls ``agent.run`` directly, reusing
``DiagDoctorAgent.execute``'s ``EventEncoder`` SSE streaming. It must be
registered BEFORE ``add_fastapi_endpoint`` so it takes precedence for
``/agent/default/run`` (otherwise CopilotKit's catch-all wins and drops
forwardedProps again). Normal diagnosis runs hit this too with empty
forwarded_props -> behaves identically to the dropped path.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

PATH = "/api/copilotkit/agent/default/run"


def register_default_agent_run_endpoint(app: FastAPI, agent: Any) -> None:
    """Register a forwardedProps-aware run endpoint for the default agent."""

    async def default_agent_run(request: Request) -> StreamingResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}

        # CopilotKit's handler drops forwardedProps; we read it here and put it
        # into RunAgentInput.forwarded_props so ag_ui_langgraph can see
        # command.resume (resume) / node_name (continue) etc.
        forwarded_props: dict[str, Any] = body.get("forwardedProps") or {}
        node_name = body.get("nodeName")
        if node_name:
            forwarded_props = {**forwarded_props, "node_name": node_name}

        run_input = RunAgentInput(
            thread_id=body.get("threadId") or str(_uuid.uuid4()),
            run_id=body.get("runId") or str(_uuid.uuid4()),
            messages=body.get("messages", []),
            state=body.get("state", {}),
            tools=[],
            context=[],
            forwarded_props=forwarded_props,
        )
        encoder = EventEncoder()

        async def _stream() -> AsyncIterator[bytes]:
            async for event in agent.run(run_input):
                yield encoder.encode(event).encode("utf-8")

        return StreamingResponse(_stream(), media_type="text/event-stream")

    # Registered before add_fastapi_endpoint -> wins over CopilotKit's catch-all
    # for /agent/default/run.
    app.add_api_route(PATH, default_agent_run, methods=["POST"])
