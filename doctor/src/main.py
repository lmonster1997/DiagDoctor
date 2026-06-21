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

init_observability()


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
