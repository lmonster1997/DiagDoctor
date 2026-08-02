"""CopilotKit runtime mount — registers the diagnosis agent on the FastAPI app."""

from __future__ import annotations

import structlog
from fastapi import FastAPI

_log = structlog.get_logger(__name__)


def mount_copilotkit(app: FastAPI) -> None:
    """Mount the CopilotKit runtime on the FastAPI app at /api/copilotkit."""
    try:
        from copilotkit.integrations.fastapi import add_fastapi_endpoint

        from copilotkit import CopilotKitRemoteEndpoint
        from src.copilotkit.agent import DiagDoctorAgent
        from src.copilotkit.middleware import CorsPreflightMiddleware, InfoCompatMiddleware
        from src.copilotkit.run_endpoint import register_default_agent_run_endpoint
        from src.engine.budget.constants import RECURSION_LIMIT
        from src.engine.nodes.diagnosis_agent import get_copilotkit_graph

        agent = DiagDoctorAgent(
            name="default",
            description="DiagDoctor — AI Bug 诊断助手",
            graph=get_copilotkit_graph(),
            config={"recursion_limit": RECURSION_LIMIT},
        )

        sdk = CopilotKitRemoteEndpoint(agents=[agent])

        # #5 F1: 自定义 /agent/default/run 端点,转发 forwardedProps(CopilotKit 的
        # handler 会丢,导致 useInterrupt resume 的 command.resume 不到后端)。
        # 必须在 add_fastapi_endpoint 之前注册,优先匹配。
        register_default_agent_run_endpoint(app, agent)

        app.add_middleware(CorsPreflightMiddleware)
        app.add_middleware(InfoCompatMiddleware)

        add_fastapi_endpoint(app, sdk, "/api/copilotkit")
        _log.info("copilotkit_runtime_mounted", agent="default")
    except ImportError:
        _log.warning("copilotkit_not_installed_skipping_runtime")
