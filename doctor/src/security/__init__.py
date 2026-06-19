"""
Security utilities for DiagDoctor.

Exports:
    - sanitizer: Path sandbox, subprocess arg validation, PII redaction.
    - secrets: SecretStr masking, enforcement helpers.
"""

from src.security.sanitizer import (
    PathSandboxError,
    safe_subprocess_args,
    sanitize_for_llm,
    sanitize_path,
)
from src.security.secrets import (
    ensure_no_leaked_secrets,
    mask,
    require_secret_fields,
)

__all__ = [
    "PathSandboxError",
    "ensure_no_leaked_secrets",
    "mask",
    "require_secret_fields",
    "safe_subprocess_args",
    "sanitize_for_llm",
    "sanitize_path",
]
