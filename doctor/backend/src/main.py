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

# --- CopilotKit Runtime (Phase 0: scaffold) ---
# Mounts the diagnosis LangGraph agent at /api/copilotkit
# so the CopilotKit React frontend can stream chat, tool calls,
# and HITL interrupts via the AG-UI protocol.
try:
    from copilotkit import CopilotKit
    from src.graph.subgraphs.diagnosis_agent import get_diagnosis_agent

    sdk = CopilotKit(
        agents={
            "diagnosis": get_diagnosis_agent(),
        },
    )
    app.mount("/api/copilotkit", sdk.app)
    import structlog
    _log = structlog.get_logger(__name__)
    _log.info("copilotkit_runtime_mounted", agent="diagnosis")
except ImportError:
    import structlog
    _log = structlog.get_logger(__name__)
    _log.warning("copilotkit_not_installed_skipping_runtime")
