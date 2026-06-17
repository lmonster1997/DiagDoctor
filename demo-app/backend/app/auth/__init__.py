"""Authentication module: JWT tokens, password hashing, and auth dependency."""

from app.auth.deps import get_current_user
from app.auth.utils import create_access_token, decode_access_token, hash_password, verify_password

__all__ = [
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "hash_password",
    "verify_password",
]
