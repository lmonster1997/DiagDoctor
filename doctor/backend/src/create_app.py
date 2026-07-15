"""FastAPI app factory.

Creates the FastAPI instance with:
- lifespan (pre-build diagnosis agent)
- CORS middleware
- OTel instrumentation
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.observability import instrument_fastapi


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Pre-build the diagnosis agent + ensure Qdrant collection at startup."""
    from src.engine.agent import get_diagnosis_agent
    from src.observability.logger import get_logger

    logger = get_logger(__name__)

    # Pre-build agent (warm cache)
    get_diagnosis_agent()

    # Ensure historical_cases collection exists with correct config
    try:
        from src.memory.long_term.qdrant_client import ensure_collection

        await ensure_collection()
    except Exception:
        logger.warning("qdrant_collection_init_failed", exc_info=True)

    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="DiagDoctor API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    instrument_fastapi(app)
    return app
