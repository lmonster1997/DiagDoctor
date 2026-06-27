"""Authentication utilities: password hashing and JWT token management."""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = settings.jwt_algorithm
SECRET_KEY = settings.jwt_secret
ACCESS_TOKEN_EXPIRE_MINUTES = settings.jwt_expire_minutes


def hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token.

    Args:
        data: Claims to encode in the token (must include 'sub' for subject).
        expires_delta: Optional custom expiry; defaults to settings.jwt_expire_minutes.

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)  # type: ignore[no-any-return]


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT access token.

    Returns:
        The token payload dict if valid, None otherwise.
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])  # type: ignore[no-any-return]
    except JWTError:
        return None
