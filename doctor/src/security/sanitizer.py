"""
Input sanitization utilities.

Provides path sandboxing, subprocess argument validation, and LLM input
sanitization (PII redaction). All functions are safe-by-default: they
raise explicit errors on invalid input rather than silently passing it through.
"""

import re
from pathlib import Path

# ── Path sandbox ─────────────────────────────────────────────────────


class PathSandboxError(ValueError):
    """Raised when a user-supplied path escapes the allowed root(s)."""


def sanitize_path(user_input: str, allowed_roots: list[Path]) -> Path:
    """
    Resolve a user-supplied file path and verify it lies within allowed roots.

    Args:
        user_input: Raw path string from user or external system.
        allowed_roots: List of canonical directories the resolved path must
                       be under.

    Returns:
        The resolved, absolute Path.

    Raises:
        PathSandboxError: If the resolved path escapes allowed_roots or is
                          otherwise invalid.
        ValueError: If allowed_roots is empty.

    Example:
        >>> sanitize_path("logs/../../etc/passwd", [Path("/app/data")])
        # raises PathSandboxError
    """
    if not allowed_roots:
        raise ValueError("allowed_roots must not be empty")

    if not user_input.strip():
        raise PathSandboxError("Path must not be empty")

    try:
        candidate = Path(user_input).resolve()
    except (OSError, RuntimeError) as exc:
        raise PathSandboxError(f"Invalid path: {user_input}") from exc

    # Check that the resolved path is under at least one allowed root
    for root in allowed_roots:
        resolved_root = root.resolve()
        try:
            candidate.relative_to(resolved_root)
            return candidate
        except ValueError:
            continue

    raise PathSandboxError(f"Path escapes allowed roots: {candidate}. Allowed: {allowed_roots}")


# ── Subprocess argument safety ───────────────────────────────────────


# Common shell metacharacters that should never appear in safe args
_SHELL_METACHARS = re.compile(r"[;&|`$(){}\[\]<>!#~*?]")


def safe_subprocess_args(args: list[str]) -> list[str]:
    """
    Validate a list of subprocess arguments for safety.

    Each argument is checked against shell metacharacters that could lead
    to command injection. This is a defence-in-depth measure; the primary
    protection should be to avoid shell=True.

    Args:
        args: List of command arguments (e.g., ["ls", "-la", "/tmp"]).

    Returns:
        The same list if all arguments pass validation.

    Raises:
        ValueError: If any argument contains dangerous shell metacharacters.

    Example:
        >>> safe_subprocess_args(["cat", "file.txt"])
        ['cat', 'file.txt']
        >>> safe_subprocess_args(["cat", "file; rm -rf /"])
        # raises ValueError
    """
    if not args:
        raise ValueError("args must not be empty")

    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            raise ValueError(f"Argument {i} is not a string: {type(arg).__name__}")
        if _SHELL_METACHARS.search(arg):
            raise ValueError(f"Argument {i} contains shell metacharacters: {arg!r}")

    return args


# ── LLM input sanitization ───────────────────────────────────────────

# Patterns for common PII
_PII_PATTERNS: list[tuple[str, str]] = [
    # Phone numbers (Chinese mobile, international)
    (r"\b1[3-9]\d{9}\b", "[PHONE]"),
    (r"\b\+\d{1,3}[\s-]?\d{4,14}\b", "[PHONE]"),
    # Email addresses
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
    # IPv4 addresses
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]"),
    # IPv6 addresses (simplified)
    (r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b", "[IPV6]"),
    # Chinese ID card numbers (18-digit)
    (r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b", "[ID_CARD]"),
    # Generic API key patterns (heuristic: long base64-ish strings)
    # Skipped — too many false positives. Use explicit allowlist instead.
]


def sanitize_for_llm(text: str) -> str:
    """
    Redact common PII from text before sending to an LLM.

    Replaces phone numbers, email addresses, and IP addresses with
    placeholders like [PHONE], [EMAIL], [IP].

    This is a best-effort measure. Always pass sensitive data through
    an explicit allowlist before letting it reach an external API.

    Args:
        text: Raw text that may contain PII.

    Returns:
        Text with recognized PII replaced by placeholder tokens.

    Example:
        >>> sanitize_for_llm("Contact alice@example.com or 1.2.3.4")
        "Contact [EMAIL] or [IP]"
    """
    result = text
    for pattern, replacement in _PII_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result
