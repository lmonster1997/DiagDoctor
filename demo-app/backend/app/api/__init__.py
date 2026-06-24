"""API route modules.

Each module defines its own APIRouter; they are collected here for
registration in main.py.
"""

from app.api.auth import router as auth_router
from app.api.client_log import router as client_log_router
from app.api.comments import router as comments_router
from app.api.projects import router as projects_router
from app.api.tasks import router as tasks_router

routers = [auth_router, client_log_router, projects_router, tasks_router, comments_router]
