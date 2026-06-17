"""SQLAlchemy 2.x async database session and engine.

Uses AsyncAttrs + DeclarativeBase for awaitable lazy loading support.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all SQLAlchemy 2.x ORM models with async support."""

    def __repr__(self) -> str:
        """Default repr using the first few columns."""
        cols = [f"{c.name}={getattr(self, c.name)!r}" for c in self.__table__.columns[:3]]
        return f"<{self.__class__.__name__}({', '.join(cols)})>"


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
