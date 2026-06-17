"""Pydantic v2 schemas for Project."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Schema for creating a new project."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None


class ProjectUpdate(BaseModel):
    """Schema for updating an existing project."""

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None


class ProjectResponse(BaseModel):
    """Schema for project data returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    created_at: datetime
