import json
import logging
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings

# --- OTel MUST be initialized before FastAPI app instantiation ---
from app.observability import (
    init_observability,
    instrument_fastapi,
    instrument_sqlalchemy,
    setup_loki_logging,
)

init_observability()

logger = structlog.get_logger(__name__)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)  # ensure http_request middleware logs reach Loki

# --- Bridge Python logging → Loki for structured diagnostics ---
try:
    setup_loki_logging()
    logger.info("Loki logging bridge enabled")
except Exception:
    logger.warning("Loki logging bridge unavailable — logs will only appear on stdout")

# --- Ensure uvicorn access logs propagate to root → Loki ---
for _name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
    _uv_logger = logging.getLogger(_name)
    _uv_logger.propagate = True
    _uv_logger.setLevel(logging.INFO)

# --- SQLAlchemy OTel instrumentation (MUST be called before engine creation) ---
instrument_sqlalchemy()
logger.info("SQLAlchemy OTel instrumentation enabled")


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


# ── Request logging middleware ────────────────────────────────────────
# Logs every HTTP request with method, path, status, duration.
# This + SQLAlchemy instrumentation + unhandled_exception handler
# together provide a rich diagnostic trail in Loki.
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    import time as _time

    t0 = _time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (_time.perf_counter() - t0) * 1000

    log.info(
        json.dumps(
            {
                "event": "http_request",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
                "client": request.client.host if request.client else "",
            },
            ensure_ascii=False,
        )
    )
    return response


# ── Global exception handler ──────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log structured error and return 500 for any unhandled exception."""
    tb = traceback.format_exc()

    # Structlog → stdout (first positional arg = event message)
    logger.error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        path=request.url.path,
        method=request.method,
        traceback=tb,
    )

    # Standard logging → Loki (JSON string)
    log.error(
        json.dumps(
            {
                "event": "unhandled_exception",
                "exc_type": type(exc).__name__,
                "exc_message": str(exc),
                "path": request.url.path,
                "method": request.method,
                "traceback": tb,
            },
            ensure_ascii=False,
        )
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
