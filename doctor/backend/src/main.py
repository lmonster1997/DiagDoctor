"""
FastAPI application entry point for DiagDoctor.

Usage:
    uv run uvicorn src.main:app --reload
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings

# --- OTel MUST be initialized before FastAPI app instantiation ---
from src.observability import init_observability, instrument_fastapi
from src.observability.logger import configure_logging

init_observability()
configure_logging(json_format=False)  # Human-readable for dev; True for prod


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown events."""
    yield


app = FastAPI(
    title="DiagDoctor API",
    version="0.1.0",
    lifespan=lifespan,
)

# --- CORS: allow frontend to call the API ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument FastAPI for OTel tracing
instrument_fastapi(app)

# --- Register API routes ---
from src.api.diagnose import router as diagnose_router  # noqa: E402
from src.api.health import router as health_router  # noqa: E402

app.include_router(health_router)
app.include_router(diagnose_router)

# --- CopilotKit Runtime ---
# Mounts the diagnosis LangGraph agent at /api/copilotkit
# so the CopilotKit React frontend can stream chat, tool calls,
# and HITL interrupts via the AG-UI protocol.
#
# Graph: bug_info → diagnosis_agent (2 nodes)
#   - bug_info: parses user message → extracts trigger_time/trace_ids
#     → auto-prefetches Loki/Tempo → normalizes into NormalizedEvidence
#   - diagnosis_agent: consumes NormalizedEvidence → ReAct loop → report
try:
    import uuid as _uuid

    from copilotkit import CopilotKitRemoteEndpoint, LangGraphAGUIAgent
    from copilotkit.integrations.fastapi import add_fastapi_endpoint
    from src.graph.copilotkit_graph import get_copilotkit_graph

    # ── Compat: LangGraphAGUIAgent has run() but SDK expects execute() ──
    from ag_ui.core import RunAgentInput

    class _DiagDoctorAgent(LangGraphAGUIAgent):
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

            from ag_ui.encoder import EventEncoder

            node_name = kwargs.get("node_name")
            forwarded_props: dict = {}
            if node_name:
                forwarded_props["node_name"] = node_name

            # Evidence collection + Langfuse tracing are handled
            # inside the graph nodes (bug_info + diagnosis_agent).
            # execute() only needs to bridge CopilotKit ↔ LangGraph.

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

    async def _agent_get_state(self, *, thread_id: str):
        """Smart resume: fresh start if previous diagnosis completed, resume if interrupted."""
        try:
            state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
            values = state.values or {}
            # If the previous diagnosis has a completed report, start fresh
            has_report = bool(values.get("report"))
            if has_report:
                return {"threadId": thread_id, "threadExists": False, "state": {}}
            # Otherwise, resume from checkpoint (e.g. agent waiting for user input)
            return {
                "threadId": thread_id,
                "threadExists": bool(values),
                "state": values,
            }
        except Exception:
            return {"threadId": thread_id, "threadExists": False, "state": {}}

    _DiagDoctorAgent.get_state = _agent_get_state  # type: ignore[attr-defined]

    _diag_agent = _DiagDoctorAgent(
        name="default",
        description="DiagDoctor — AI Bug 诊断助手",
        graph=get_copilotkit_graph(),
        config={"recursion_limit": 80},
    )

    sdk = CopilotKitRemoteEndpoint(agents=[_diag_agent])

    # CORS preflight workaround: add_fastapi_endpoint registers
    # routes with methods=["OPTIONS",...], which routes OPTIONS
    # to the copilotkit handler (→ 400).  This middleware catches
    # OPTIONS before routing and returns 200.
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class _CorsPreflightMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "OPTIONS" and request.url.path.startswith("/api/copilotkit"):
                return Response(status_code=200)
            return await call_next(request)

    app.add_middleware(_CorsPreflightMiddleware)

    # CopilotKit protocol compat: Python SDK 0.1.94 returns agents as an
    # array [{name, description, type}], but the React frontend (1.62.x)
    # expects agents as an object {name: {description, capabilities}}.
    # This middleware rewrites the /info response (REST mode) AND the
    # single-endpoint "method":"info" response (auto/single mode) on the fly.
    import json as _json

    def _build_info_response() -> dict:
        """Build /info response with agents as object (compat with frontend 1.62.x)."""
        return {
            "actions": [],
            "agents": {
                "default": {
                    "description": "DiagDoctor — AI Bug 诊断助手",
                    "capabilities": {},
                }
            },
            "sdkVersion": "0.1.94",
        }

    class _CopilotKitInfoCompatMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path.rstrip("/")
            import re as _re

            # Python SDK 0.1.94 doesn't support GET /threads (REST mode).
            if path == "/api/copilotkit/threads":
                return Response(
                    content=_json.dumps({"threads": []}).encode("utf-8"),
                    status_code=200,
                    media_type="application/json",
                )

            # ── /agent/{name}/connect — CopilotKit 1.62.x SSE handshake ──
            # The React frontend (1.62.x) probes this endpoint to establish
            # a streaming connection.  Python SDK 0.1.94 doesn't implement it,
            # and a 404 causes the frontend to fall back to POST /agent/{name}
            # with an empty message, triggering unwanted agent execution on
            # page load.  Return an empty SSE stream to satisfy the handshake
            # without starting the graph.
            if _re.match(r"^/api/copilotkit/agent/([a-zA-Z0-9_-]+)/connect$", path):
                return Response(
                    content=b"",
                    status_code=200,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )

            # Python SDK 0.1.94 requires POST with body for /info,
            # but CopilotKit REST mode uses GET.  Return info directly.
            if request.method == "GET" and path == "/api/copilotkit/info":
                return Response(
                    content=_json.dumps(_build_info_response(), ensure_ascii=False).encode("utf-8"),
                    status_code=200,
                    media_type="application/json",
                )

            response = await call_next(request)

            # ── Fix content-type for agent execute streaming responses ──
            # ``handle_execute_agent`` wraps the SSE stream in
            # ``StreamingResponse(media_type="application/json")``, but our
            # ``_serialize_events`` emits SSE format (``data: {...}\n\n``).
            # Rewrite the header to ``text/event-stream`` so the CopilotKit
            # React frontend's SSE parser handles the stream correctly.
            ctype = response.headers.get("content-type", "")
            if _re.match(r"^/api/copilotkit/agent/([a-zA-Z0-9_-]+)$", path):
                if "text/event-stream" not in ctype and request.method == "POST":
                    response.headers["content-type"] = "text/event-stream; charset=utf-8"

            # Only transform JSON (non-streaming) responses for info endpoints
            if "text/event-stream" in response.headers.get("content-type", ""):
                return response  # never buffer SSE streaming responses

            # REST mode: /api/copilotkit/info  OR  single mode: /api/copilotkit
            if path in ("/api/copilotkit/info", "/api/copilotkit"):
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk
                try:
                    data = _json.loads(body)
                    agents_array = data.get("agents", [])
                    if isinstance(agents_array, list) and agents_array:
                        data["agents"] = {
                            agent["name"]: {
                                "description": agent.get("description", ""),
                                "capabilities": {},
                            }
                            for agent in agents_array
                            if "name" in agent
                        }
                        return Response(
                            content=_json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            status_code=response.status_code,
                            media_type="application/json",
                        )
                except Exception:
                    pass
                # Body consumed — reconstruct so downstream sees the original response
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers={k: v for k, v in response.headers.items()
                             if k.lower() != "content-length"},
                    media_type=response.headers.get("content-type", "application/json"),
                )
            return response

    app.add_middleware(_CopilotKitInfoCompatMiddleware)

    add_fastapi_endpoint(app, sdk, "/api/copilotkit")
    import structlog
    _log = structlog.get_logger(__name__)
    _log.info("copilotkit_runtime_mounted", agent="default")
except ImportError:
    import structlog
    _log = structlog.get_logger(__name__)
    _log.warning("copilotkit_not_installed_skipping_runtime")
