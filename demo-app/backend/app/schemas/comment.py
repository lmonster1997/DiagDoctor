"""Pydantic v2 schemas for Comment."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CommentCreate(BaseModel):
    """Schema for creating a new comment."""

    content: str = Field(..., min_length=1)


class CommentUpdate(BaseModel):
    """Schema for updating an existing comment."""

    content: str | None = Field(None, min_length=1)


class CommentResponse(BaseModel):
    """Schema for comment data returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    author_id: uuid.UUID
    content: str
    created_at: datetime
