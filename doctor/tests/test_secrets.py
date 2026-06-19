"""
Tests for src.security.secrets — SecretStr masking and enforcement.
"""

from dataclasses import dataclass

import pytest
from pydantic import BaseModel, SecretStr

from src.security.secrets import (
    ensure_no_leaked_secrets,
    mask,
    require_secret_fields,
)


class TestMask:
    """Tests for the mask function."""

    def test_mask_basic(self) -> None:
        """Mask should show first and last 3 chars with *** in between."""
        result = mask(SecretStr("abcdefghijklmno"), visible_chars=3)
        assert result == "abc***mno"

    def test_mask_custom_visible_chars(self) -> None:
        """Custom visible_chars should be respected."""
        result = mask(SecretStr("1234567890ab"), visible_chars=4)
        assert result == "1234***90ab"

    def test_mask_short_secret(self) -> None:
        """Short secret should still be masked safely."""
        result = mask(SecretStr("abc"), visible_chars=3)
        # visible_chars * 2 >= len, so visible_chars reduced
        assert "***" in result
        assert len(result) < 10

    def test_mask_empty_secret(self) -> None:
        """Empty secret should return '***'."""
        result = mask(SecretStr(""), visible_chars=3)
        assert result == "***"

    def test_mask_negative_visible_chars_raises(self) -> None:
        """Negative visible_chars should raise ValueError."""
        with pytest.raises(ValueError, match=">= 0"):
            mask(SecretStr("secret"), visible_chars=-1)

    def test_mask_zero_visible_chars(self) -> None:
        """Zero visible_chars should be allowed."""
        result = mask(SecretStr("mykey"), visible_chars=0)
        assert result == "***"


class TestRequireSecretFields:
    """Tests for require_secret_fields validation."""

    def test_all_fields_are_secretstr(self) -> None:
        """Should pass when all specified fields are SecretStr."""

        class GoodConfig(BaseModel):
            api_key: SecretStr
            db_password: SecretStr
            name: str

        obj = GoodConfig(api_key=SecretStr("k"), db_password=SecretStr("p"), name="x")
        require_secret_fields(obj, ["api_key", "db_password"])
        # No exception means success

    def test_plain_str_field_raises(self) -> None:
        """Should raise TypeError if a required field is plain str."""

        class BadConfig(BaseModel):
            api_key: str
            name: str

        obj = BadConfig(api_key="plain-text-key", name="x")
        with pytest.raises(TypeError, match="must be a SecretStr"):
            require_secret_fields(obj, ["api_key"])

    def test_missing_field(self) -> None:
        """If the field is not on the object, it should be treated as non-SecretStr."""

        class Config(BaseModel):
            name: str

        obj = Config(name="x")
        with pytest.raises(TypeError, match="must be a SecretStr"):
            require_secret_fields(obj, ["api_key"])


class TestEnsureNoLeakedSecrets:
    """Tests for ensure_no_leaked_secrets."""

    def test_secretstr_field_passes_dataclass(self) -> None:
        """SecretStr fields should not raise."""

        @dataclass
        class Good:
            api_key: SecretStr
            name: str

        obj = Good(api_key=SecretStr("k"), name="n")
        ensure_no_leaked_secrets(obj)
        # No exception means success

    def test_plain_str_api_key_raises_dataclass(self) -> None:
        """Plain str field named api_key should raise TypeError."""

        @dataclass
        class Bad:
            api_key: str

        obj = Bad(api_key="sk-plaintext")
        with pytest.raises(TypeError, match="(?i)secretstr"):
            ensure_no_leaked_secrets(obj)

    def test_plain_str_secret_raises_dataclass(self) -> None:
        """Plain str field named 'secret' should raise TypeError."""

        @dataclass
        class Bad:
            secret: str

        obj = Bad(secret="my-secret")
        with pytest.raises(TypeError, match="(?i)secretstr"):
            ensure_no_leaked_secrets(obj)

    def test_plain_str_password_raises_dataclass(self) -> None:
        """Plain str field named 'password' should raise TypeError."""

        @dataclass
        class Bad:
            password: str

        obj = Bad(password="123456")
        with pytest.raises(TypeError, match="(?i)secretstr"):
            ensure_no_leaked_secrets(obj)

    def test_empty_string_field_passes(self) -> None:
        """An empty string for a field with a sensitive name should pass."""

        @dataclass
        class Config:
            api_key: str
            name: str

        obj = Config(api_key="", name="n")
        ensure_no_leaked_secrets(obj)
        # Should not raise for empty string

    def test_none_field_passes(self) -> None:
        """A None value should pass."""

        @dataclass
        class Config:
            token: str | None
            name: str

        obj = Config(token=None, name="n")
        ensure_no_leaked_secrets(obj)
        # Should not raise for None
