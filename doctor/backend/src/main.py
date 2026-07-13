"""
DiagDoctor API — application entry point.

Usage:
    uv run uvicorn src.main:app --reload
"""

# ── OTel + logging MUST be initialized before FastAPI app instantiation ──
from src.observability import init_observability
from src.observability.logger import configure_logging

init_observability()
configure_logging(json_format=False)

# ── Assemble the application ──
from src.create_app import create_app
from src.api.routes import register_routes
from src.copilotkit.mount import mount_copilotkit

app = create_app()
register_routes(app)
mount_copilotkit(app)
