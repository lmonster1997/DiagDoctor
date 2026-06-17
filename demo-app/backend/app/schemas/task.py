"""Pydantic v2 schemas for Task."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.task import TaskStatus


class TaskCreate(BaseModel):
    """Schema for creating a new task."""

    title: str = Field(..., min_length=1, max_length=300)
    description: str | None = None
    status: TaskStatus = TaskStatus.todo
    priority: int = Field(0, ge=0)
    assignee_id: uuid.UUID | None = None
    due_date: datetime | None = None


class TaskUpdate(BaseModel):
    """Schema for updating an existing task."""

    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = None
    status: TaskStatus | None = None
    priority: int | None = Field(None, ge=0)
    assignee_id: uuid.UUID | None = None
    due_date: datetime | None = None


class TaskResponse(BaseModel):
    """Schema for task data returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    priority: int
    assignee_id: uuid.UUID | None
    due_date: datetime | None
    created_at: datetime
    updated_at: datetime
