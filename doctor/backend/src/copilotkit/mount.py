"""CopilotKit runtime mount — registers the diagnosis agent on the FastAPI app."""

from __future__ import annotations

import structlog

from fastapi import FastAPI

_log = structlog.get_logger(__name__)


def mount_copilotkit(app: FastAPI) -> None:
    """Mount the CopilotKit runtime on the FastAPI app at /api/copilotkit."""
    try:
        from copilotkit import CopilotKitRemoteEndpoint
        from copilotkit.integrations.fastapi import add_fastapi_endpoint

        from src.copilotkit.agent import DiagDoctorAgent
        from src.copilotkit.middleware import CorsPreflightMiddleware, InfoCompatMiddleware
        from src.engine.nodes.diagnosis_agent import get_copilotkit_graph

        agent = DiagDoctorAgent(
            name="default",
            description="DiagDoctor — AI Bug 诊断助手",
            graph=get_copilotkit_graph(),
            config={"recursion_limit": 80},
        )

        sdk = CopilotKitRemoteEndpoint(agents=[agent])

        app.add_middleware(CorsPreflightMiddleware)
        app.add_middleware(InfoCompatMiddleware)

        add_fastapi_endpoint(app, sdk, "/api/copilotkit")
        _log.info("copilotkit_runtime_mounted", agent="default")
    except ImportError:
        _log.warning("copilotkit_not_installed_skipping_runtime")
