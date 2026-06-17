"""Pydantic v2 schemas for Tag."""

from pydantic import BaseModel, ConfigDict, Field


class TagCreate(BaseModel):
    """Schema for creating a new tag."""

    name: str = Field(..., min_length=1, max_length=50)
    color: str = Field("#6b7280", pattern=r"^#[0-9a-fA-F]{6}$")


class TagUpdate(BaseModel):
    """Schema for updating an existing tag."""

    name: str | None = Field(None, min_length=1, max_length=50)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class TagResponse(BaseModel):
    """Schema for tag data returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str
