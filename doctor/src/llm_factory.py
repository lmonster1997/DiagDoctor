"""LLM factory — resolve the appropriate model for each node role."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.config import settings

NodeRole = Literal["triage", "specialist", "diagnosis", "default", "judge"]

# ── Provider detection ──────────────────────────────────────────────
_REASONING_KEYWORDS = ("o1", "o3", "qwq", "gemini-thinking")
_DEEPSEEK_KEYWORDS = ("deepseek",)


def _is_deepseek(model: str, base_url: str) -> bool:
    combined = f"{model.lower()} {base_url.lower()}"
    return any(kw in combined for kw in _DEEPSEEK_KEYWORDS)


def _is_native_reasoning(model: str, base_url: str) -> bool:
    if _is_deepseek(model, base_url):
        return False
    combined = f"{model.lower()} {base_url.lower()}"
    return any(kw in combined for kw in _REASONING_KEYWORDS)


# ═════════════════════════════════════════════════════════════════════
# DeepSeekChatOpenAI — 注入 thinking mode（via extra_body）
# ═════════════════════════════════════════════════════════════════════


class DeepSeekChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that injects DeepSeek ``thinking`` via ``extra_body``.

    OpenAI Python SDK rejects unknown top-level parameters, but accepts
    provider-specific fields under ``extra_body``.  We hook into
    ``_get_request_payload`` and nest ``thinking`` under ``extra_body``
    so the SDK passes it through to the DeepSeek API.

    Config (``.env``)::

        LLM_DEEPSEEK_THINKING=false   # default: disabled for agent tasks
    """

    deepseek_thinking: bool = False

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Nest under extra_body — the OpenAI SDK accepts this and passes
        # it as-is in the JSON request body to the provider.
        existing_extra = payload.get("extra_body", {})
        if isinstance(existing_extra, str):
            try:
                existing_extra = json.loads(existing_extra)
            except json.JSONDecodeError:
                existing_extra = {}
        existing_extra["thinking"] = {
            "type": "enabled" if self.deepseek_thinking else "disabled"
        }
        payload["extra_body"] = existing_extra
        return payload


# ═════════════════════════════════════════════════════════════════════
# Factory
# ═════════════════════════════════════════════════════════════════════


@lru_cache(maxsize=6)
def get_llm_for_role(role: NodeRole) -> BaseChatModel:
    """Get a configured LLM instance for a role."""
    if role == "judge":
        api_key = (
            settings.llm_judge_api_key.get_secret_value()
            or settings.llm_api_key.get_secret_value()
        )
        base_url = settings.llm_judge_base_url or settings.llm_base_url
        model = settings.llm_judge_model or settings.llm_specialist_model or settings.llm_model
        temp = settings.llm_judge_temperature
        max_tok = settings.llm_judge_max_tokens
    elif role == "triage":
        model = settings.llm_triage_model or settings.llm_model
        temp = settings.llm_triage_temperature
        max_tok = settings.llm_triage_max_tokens
        api_key = settings.llm_api_key.get_secret_value()
        base_url = settings.llm_base_url
    elif role in ("specialist", "diagnosis"):
        model = settings.llm_specialist_model or settings.llm_model
        temp = settings.llm_specialist_temperature
        max_tok = settings.llm_specialist_max_tokens
        api_key = settings.llm_api_key.get_secret_value()
        base_url = settings.llm_base_url
    else:
        model = settings.llm_model
        temp = settings.llm_temperature
        max_tok = settings.llm_max_tokens
        api_key = settings.llm_api_key.get_secret_value()
        base_url = settings.llm_base_url

    # ── DeepSeek → subclass with thinking control ────────────────
    if _is_deepseek(model, base_url):
        return DeepSeekChatOpenAI(
            model=model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            temperature=temp,
            max_completion_tokens=max_tok,
            timeout=120,
            max_retries=2,
            deepseek_thinking=bool(
                getattr(settings, "llm_deepseek_thinking", False)
            ),
        )

    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=temp,
        max_completion_tokens=max_tok,
        timeout=120,
        max_retries=2,
    )


def get_model_name_for_role(role: NodeRole) -> str:
    if role == "triage":
        return settings.llm_triage_model or settings.llm_model
    elif role in ("specialist", "diagnosis"):
        return settings.llm_specialist_model or settings.llm_model
    return settings.llm_model


def clear_llm_cache() -> None:
    get_llm_for_role.cache_clear()


def is_reasoning_model() -> bool:
    model = settings.llm_specialist_model or settings.llm_model
    return _is_native_reasoning(model, settings.llm_base_url)


def is_deepseek_model() -> bool:
    model = settings.llm_specialist_model or settings.llm_model
    return _is_deepseek(model, settings.llm_base_url)
