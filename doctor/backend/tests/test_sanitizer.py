"""
Tests for src.security.sanitizer — input sanitization utilities.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.security.sanitizer import (
    PathSandboxError,
    safe_subprocess_args,
    sanitize_for_llm,
    sanitize_path,
)


class TestSanitizePath:
    """Tests for path sandbox."""

    def test_valid_path_within_root(self) -> None:
        """A path within an allowed root should be returned resolved."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "subdir").mkdir()
            (root / "subdir" / "file.txt").touch()

            result = sanitize_path(f"{tmp}/subdir/file.txt", [root])
            assert result == (root / "subdir" / "file.txt")

    def test_valid_path_multiple_roots_second_matches(self) -> None:
        """Path should match if it's under any of the allowed roots."""
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            root1 = Path(tmp1).resolve()
            root2 = Path(tmp2).resolve()
            (root2 / "data.txt").touch()

            result = sanitize_path(f"{tmp2}/data.txt", [root1, root2])
            assert result == root2 / "data.txt"

    def test_path_escapes_root(self) -> None:
        """A path that resolves outside allowed roots should raise."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with pytest.raises(PathSandboxError, match="escapes allowed roots"):
                sanitize_path(f"{tmp}/../../etc/passwd", [root])

    def test_traversal_with_dotdot(self, tmp_path: Path) -> None:
        """Directory traversal attempt should be caught."""
        root = tmp_path / "allowed"
        root.mkdir()
        (tmp_path / "forbidden.txt").touch()

        with pytest.raises(PathSandboxError):
            sanitize_path(str(root / "../forbidden.txt"), [root])

    def test_empty_input_raises(self) -> None:
        """Empty path string should raise PathSandboxError."""
        with pytest.raises(PathSandboxError, match="must not be empty"):
            sanitize_path("   ", [Path("/tmp")])

    def test_empty_allowed_roots_raises(self) -> None:
        """Empty allowed_roots list should raise ValueError."""
        with pytest.raises(ValueError, match="allowed_roots must not be empty"):
            sanitize_path("/foo/bar", [])


class TestSafeSubprocessArgs:
    """Tests for subprocess argument validation."""

    def test_valid_args_pass_through(self) -> None:
        """Clean args should be returned unchanged."""
        args = ["ls", "-la", "/tmp"]
        result = safe_subprocess_args(args)
        assert result == args

    def test_single_valid_arg(self) -> None:
        """Single clean arg should pass."""
        assert safe_subprocess_args(["cat"]) == ["cat"]

    def test_empty_args_raises(self) -> None:
        """Empty args list should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            safe_subprocess_args([])

    def test_pipe_character_rejected(self) -> None:
        """Pipe character should be rejected."""
        with pytest.raises(ValueError, match="shell metacharacters"):
            safe_subprocess_args(["echo", "hello | rm"])

    def test_semicolon_rejected(self) -> None:
        """Semicolon should be rejected."""
        with pytest.raises(ValueError, match="shell metacharacters"):
            safe_subprocess_args(["ls; cat /etc/passwd"])

    def test_dollar_substitution_rejected(self) -> None:
        """Dollar sign should be rejected."""
        with pytest.raises(ValueError, match="shell metacharacters"):
            safe_subprocess_args(["echo", "$(whoami)"])

    def test_backtick_rejected(self) -> None:
        """Backtick should be rejected."""
        with pytest.raises(ValueError, match="shell metacharacters"):
            safe_subprocess_args(["echo", "`whoami`"])

    def test_non_string_arg_raises(self) -> None:
        """Non-string argument should raise ValueError."""
        with pytest.raises(ValueError, match="not a string"):
            safe_subprocess_args(["cmd", 123])  # type: ignore[list-item]

    def test_url_with_special_chars_rejected(self) -> None:
        """URL with & should be rejected."""
        with pytest.raises(ValueError, match="shell metacharacters"):
            safe_subprocess_args(["curl", "http://example.com?a=1&b=2"])


class TestSanitizeForLLM:
    """Tests for PII redaction."""

    def test_phone_number_redacted(self) -> None:
        """Chinese mobile number should be replaced with [PHONE]."""
        result = sanitize_for_llm("Call me at 13800138000.")
        assert "[PHONE]" in result
        assert "13800138000" not in result

    def test_email_redacted(self) -> None:
        """Email address should be replaced with [EMAIL]."""
        result = sanitize_for_llm("Contact alice@example.com for help.")
        assert "[EMAIL]" in result
        assert "alice@example.com" not in result

    def test_ipv4_redacted(self) -> None:
        """IPv4 address should be replaced with [IP]."""
        result = sanitize_for_llm("Server at 192.168.1.1 is down.")
        assert "[IP]" in result
        assert "192.168.1.1" not in result

    def test_multiple_pii_redacted(self) -> None:
        """Multiple PII types should all be redacted."""
        text = "Email bob@test.com, call 13912345678, IP 10.0.0.1"
        result = sanitize_for_llm(text)
        assert "[EMAIL]" in result
        assert "[PHONE]" in result
        assert "[IP]" in result

    def test_clean_text_unchanged(self) -> None:
        """Text without PII should remain unchanged."""
        text = "The application returned a 500 error."
        assert sanitize_for_llm(text) == text

    def test_empty_string(self) -> None:
        """Empty string should return empty string."""
        assert sanitize_for_llm("") == ""

    def test_id_card_redacted(self) -> None:
        """Chinese ID card number should be replaced."""
        # Valid-format 18-digit ID number (fictional)
        result = sanitize_for_llm("ID: 110101199001011234")
        assert "[ID_CARD]" in result
        assert "110101199001011234" not in result
