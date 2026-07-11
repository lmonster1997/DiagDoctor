"""Comments API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.database import get_db
from app.models.comment import Comment
from app.models.task import Task
from app.models.user import User
from app.schemas.comment import CommentCreate, CommentResponse

router = APIRouter(prefix="/api", tags=["comments"])


@router.post(
    "/tasks/{task_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    task_id: uuid.UUID,
    payload: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Comment:
    """Add a comment to a task."""
    # Verify the task exists
    task_result = await db.execute(select(Task).where(Task.id == task_id))
    if task_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    comment = Comment(
        id=uuid.uuid4(),
        task_id=task_id,
        author_id=current_user.id,
        content=payload.content,
    )
    db.add(comment)
    await db.flush()
    await db.refresh(comment)
    return comment
