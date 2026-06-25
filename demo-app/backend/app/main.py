"""
FastAPI application entry point for TaskFlow backend.

Usage:
    uv run uvicorn app.main:app --reload
"""

import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings

# --- OTel MUST be initialized before FastAPI app instantiation ---
from app.observability import init_observability, instrument_fastapi, setup_loki_logging

init_observability()

logger = structlog.get_logger(__name__)

# --- Bridge Python logging → Loki for structured diagnostics ---
try:
    setup_loki_logging()
    logger.info("Loki logging bridge enabled")
except Exception:
    logger.warning("Loki logging bridge unavailable — logs will only appear on stdout")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown events."""
    yield


app = FastAPI(
    title="TaskFlow API",
    version="0.1.0",
    lifespan=lifespan,
)

# --- CORS: allow frontend dev server to call the API ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument FastAPI for OTel tracing
instrument_fastapi(app)


# ── Global exception handler ──────────────────────────────────────────
# Catches unhandled exceptions (IntegrityError, etc.) and logs a
# structured ``unhandled_exception`` event that the evidence collector
# and Doctor agent can search for in Loki / Tempo.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log structured error and return 500 for any unhandled exception."""
    logger.error(
        "unhandled_exception",
        event="unhandled_exception",
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        path=request.url.path,
        method=request.method,
        traceback=traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# --- Register API routes ---
from app.api import routers  # noqa: E402

for router in routers:
    app.include_router(router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
