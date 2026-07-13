"""CopilotKit protocol-compat middlewares.

- CORS preflight for /api/copilotkit OPTIONS
- Info response format rewriting (array→object, frontend 1.62.x compat)
- GET /threads stub
- /agent/{name}/connect SSE handshake stub
- SSE content-type rewriting
"""

from __future__ import annotations

import json as _json
import re as _re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


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


class CorsPreflightMiddleware(BaseHTTPMiddleware):
    """Return 200 for OPTIONS /api/copilotkit (preflight workaround)."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" and request.url.path.startswith("/api/copilotkit"):
            return Response(status_code=200)
        return await call_next(request)


class InfoCompatMiddleware(BaseHTTPMiddleware):
    """Rewrite CopilotKit SDK responses for frontend 1.62.x compatibility."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/")

        # GET /threads stub
        if path == "/api/copilotkit/threads":
            return Response(
                content=_json.dumps({"threads": []}).encode("utf-8"),
                status_code=200,
                media_type="application/json",
            )

        # SSE handshake for /agent/{name}/connect
        if _re.match(r"^/api/copilotkit/agent/([a-zA-Z0-9_-]+)/connect$", path):
            return Response(
                content=b"",
                status_code=200,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        # GET /info → return compat response directly
        if request.method == "GET" and path == "/api/copilotkit/info":
            return Response(
                content=_json.dumps(_build_info_response(), ensure_ascii=False).encode("utf-8"),
                status_code=200,
                media_type="application/json",
            )

        response = await call_next(request)

        # Fix content-type for SSE agent responses
        ctype = response.headers.get("content-type", "")
        if _re.match(r"^/api/copilotkit/agent/([a-zA-Z0-9_-]+)$", path) and (
            "text/event-stream" not in ctype and request.method == "POST"
        ):
            response.headers["content-type"] = "text/event-stream; charset=utf-8"

        if "text/event-stream" in response.headers.get("content-type", ""):
            return response

        # Rewrite /info response: agents array → object
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
            return Response(
                content=body,
                status_code=response.status_code,
                headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() != "content-length"
                },
                media_type=response.headers.get("content-type", "application/json"),
            )

        return response
