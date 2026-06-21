"""
Secret and credential masking utilities.

Provides safe handling of Pydantic SecretStr values and validation
that configuration classes never expose raw secrets.
"""

from pydantic import SecretStr


def mask(secret: SecretStr, visible_chars: int = 3) -> str:
    """
    Return a masked representation of a secret, showing only the first
    and last `visible_chars` characters.

    Args:
        secret: The SecretStr to mask.
        visible_chars: Number of characters to show at each end.

    Returns:
        A string like "abc***xyz" for a 10-char secret with visible_chars=3.

    Raises:
        ValueError: If visible_chars is negative or greater than half the
                    secret length.

    Example:
        >>> from pydantic import SecretStr
        >>> mask(SecretStr("sk-1234567890abcdef"))
        'sk-***def'
    """
    raw = secret.get_secret_value()
    length = len(raw)

    if visible_chars < 0:
        raise ValueError("visible_chars must be >= 0")

    if length == 0:
        return "***"

    if visible_chars == 0:
        return "***"

    if visible_chars * 2 >= length:
        # For very short secrets, just show 1 char at each end
        visible_chars = max(1, length // 4)

    return f"{raw[:visible_chars]}***{raw[-visible_chars:]}"


def require_secret_fields(
    obj: object,
    field_names: list[str],
) -> None:
    """
    Validate that specified fields on an object are SecretStr instances.

    Use this in __post_init__ or model_validator to enforce that
    sensitive configuration fields are never stored as plain strings.

    Args:
        obj: The object (e.g., a Pydantic BaseModel or dataclass) to check.
        field_names: Names of fields that must be SecretStr.

    Raises:
        TypeError: If any specified field is not a SecretStr instance.

    Example:
        >>> from pydantic import BaseModel
        >>> class MyConfig(BaseModel):
        ...     api_key: SecretStr
        ...     db_url: str
        >>> require_secret_fields(MyConfig(api_key=SecretStr("k"), db_url="x"), ["api_key"])
        # passes
        >>> require_secret_fields(MyConfig(api_key=SecretStr("k"), db_url="x"), ["db_url"])
        # raises TypeError
    """
    for name in field_names:
        value = getattr(obj, name, None)
        if not isinstance(value, SecretStr):
            raise TypeError(
                f"Field '{name}' must be a SecretStr, got {type(value).__name__}. "
                f"Wrap the value with pydantic.SecretStr()."
            )


def ensure_no_leaked_secrets(obj: object) -> None:
    """
    Check that no field name containing 'secret', 'password', 'token',
    or 'key' (case-insensitive) is a plain str.

    Raises TypeError if a likely-secret field is a plain string.

    Args:
        obj: The object to inspect.

    Example:
        >>> from dataclasses import dataclass
        >>> @dataclass
        ... class BadConfig:
        ...     api_key: str = "sk-plaintext"
        >>> ensure_no_leaked_secrets(BadConfig())
        # raises TypeError
    """
    sensitive_patterns = ("secret", "password", "token", "key")
    for attr_name in dir(obj):
        if any(p in attr_name.lower() for p in sensitive_patterns):
            value = getattr(obj, attr_name, None)
            if isinstance(value, str) and value:
                raise TypeError(
                    f"Field '{attr_name}' appears to be a secret but is a plain str. "
                    f"Use pydantic.SecretStr instead."
                )
