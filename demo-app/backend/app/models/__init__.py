"""SQLAlchemy models — import all so Alembic can discover them."""

from app.models.comment import Comment
from app.models.project import Project
from app.models.tag import Tag, TaskTag
from app.models.task import Task, TaskStatus
from app.models.user import User

__all__ = [
    "Comment",
    "Project",
    "Tag",
    "Task",
    "TaskStatus",
    "TaskTag",
    "User",
]
