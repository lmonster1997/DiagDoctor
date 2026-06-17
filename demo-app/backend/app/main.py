"""
FastAPI application entry point for TaskFlow backend.

Usage:
    uv run uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

# --- OTel MUST be initialized before FastAPI app instantiation ---
from app.observability import init_observability, instrument_fastapi

init_observability()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    # Startup
    yield
    # Shutdown


app = FastAPI(
    title="TaskFlow API",
    version="0.1.0",
    lifespan=lifespan,
)

# Instrument FastAPI for OTel tracing
instrument_fastapi(app)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
