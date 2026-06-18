"""
FastAPI application entry point for TaskFlow backend.

Usage:
    uv run uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

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

# --- Register API routes ---
from app.api import routers  # noqa: E402

for router in routers:
    app.include_router(router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
