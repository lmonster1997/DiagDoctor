"""Centralised REST route registration."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    """Register all REST API routers on the app."""
    from src.api.diagnose import router as diagnose_router
    from src.api.feedback import router as feedback_router
    from src.api.health import router as health_router

    app.include_router(health_router)
    app.include_router(diagnose_router)
    app.include_router(feedback_router)
