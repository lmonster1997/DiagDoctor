"""Unit tests for embeddings module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.knowledge.embeddings import get_embeddings, reset_embeddings_cache


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Reset embeddings cache before each test."""
    reset_embeddings_cache()


class TestGetEmbeddings:
    """Tests for get_embeddings factory function."""

    @patch("langchain_openai.OpenAIEmbeddings")
    @patch("src.knowledge.embeddings.settings")
    def test_uses_openai_when_api_key_set(
        self, mock_settings: MagicMock, mock_openai: MagicMock
    ) -> None:
        """Should use OpenAI embeddings when API key is configured."""
        mock_settings.llm_api_key.get_secret_value.return_value = "sk-test-key"
        mock_settings.embedding_base_url = ""
        mock_settings.llm_base_url = "https://api.openai.com/v1"
        mock_settings.embedding_model = "text-embedding-3-small"

        mock_openai.return_value = MagicMock()
        result = get_embeddings()
        mock_openai.assert_called_once()
        assert result is not None

    @patch("langchain_openai.OpenAIEmbeddings")
    @patch("src.knowledge.embeddings.settings")
    def test_uses_custom_embedding_base_url(
        self, mock_settings: MagicMock, mock_openai: MagicMock
    ) -> None:
        """Should pass custom EMBEDDING_BASE_URL when set."""
        mock_settings.llm_api_key.get_secret_value.return_value = "sk-test-key"
        mock_settings.embedding_base_url = "https://custom-embeddings.example.com/v1"
        mock_settings.llm_base_url = "https://api.openai.com/v1"
        mock_settings.embedding_model = "text-embedding-3-small"

        mock_openai.return_value = MagicMock()
        get_embeddings()

        call_kwargs = mock_openai.call_args[1]
        assert call_kwargs["openai_api_base"] == "https://custom-embeddings.example.com/v1"

    @patch("langchain_community.embeddings.HuggingFaceEmbeddings")
    @patch("src.knowledge.embeddings.settings")
    def test_falls_back_to_local_when_no_api_key(
        self, mock_settings: MagicMock, mock_hf: MagicMock
    ) -> None:
        """Should fall back to HuggingFace when no API key is set."""
        mock_settings.llm_api_key.get_secret_value.return_value = ""
        mock_settings.embedding_base_url = ""
        mock_settings.llm_base_url = ""
        mock_settings.embedding_model = "text-embedding-3-small"

        mock_hf.return_value = MagicMock()
        result = get_embeddings()
        mock_hf.assert_called_once()
        assert result is not None

    @patch("langchain_community.embeddings.HuggingFaceEmbeddings")
    @patch("langchain_openai.OpenAIEmbeddings")
    @patch("src.knowledge.embeddings.settings")
    def test_falls_back_when_openai_init_fails(
        self,
        mock_settings: MagicMock,
        mock_openai: MagicMock,
        mock_hf: MagicMock,
    ) -> None:
        """Should fall back to local when OpenAI init raises an exception."""
        mock_settings.llm_api_key.get_secret_value.return_value = "sk-test-key"
        mock_settings.embedding_base_url = ""
        mock_settings.llm_base_url = "https://api.openai.com/v1"
        mock_settings.embedding_model = "text-embedding-3-small"

        mock_openai.side_effect = Exception("Connection refused")
        mock_hf.return_value = MagicMock()
        result = get_embeddings()
        mock_hf.assert_called_once()
        assert result is not None

    @patch("langchain_community.embeddings.HuggingFaceEmbeddings")
    @patch("src.knowledge.embeddings.settings")
    def test_caches_result(self, mock_settings: MagicMock, mock_hf: MagicMock) -> None:
        """Should cache the embeddings instance after first creation."""
        mock_settings.llm_api_key.get_secret_value.return_value = ""
        mock_settings.embedding_base_url = ""
        mock_settings.llm_base_url = ""

        mock_hf.return_value = MagicMock()
        result1 = get_embeddings()
        result2 = get_embeddings()
        # Should only be called once due to caching
        mock_hf.assert_called_once()
        assert result1 is result2
