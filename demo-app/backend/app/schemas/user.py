"""Pydantic v2 schemas for User."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Schema for creating a new user."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=8, max_length=128, description="Plain-text password")
    display_name: str = Field(..., min_length=1, max_length=100)


class UserUpdate(BaseModel):
    """Schema for updating an existing user."""

    email: EmailStr | None = Field(None, description="New email address")
    display_name: str | None = Field(None, min_length=1, max_length=100)
    is_active: bool | None = None


class UserResponse(BaseModel):
    """Schema for user data returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime
    is_active: bool
