"""
LLM factory — resolve the appropriate model for each node role.

Implements the tiered-model strategy:
- **Triage**: cheapest/fastest model (classification is simple 6-category)
- **Specialist**: strongest model available (needs code-level reasoning)
- **Default** (synthesis/reporter): standard model (aggregation/formatting)

All models share the same ``llm_api_key`` / ``llm_base_url``.
Each role falls back to ``llm_model`` if its specific model is not configured.

Usage::

    from src.llm_factory import get_llm_for_role

    triage_llm = get_llm_for_role("triage")
    specialist_llm = get_llm_for_role("specialist")
    default_llm = get_llm_for_role("default")
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langchain_openai import ChatOpenAI

from src.config import settings

NodeRole = Literal["triage", "specialist", "diagnosis", "default"]


@lru_cache(maxsize=3)
def get_llm_for_role(role: NodeRole) -> ChatOpenAI:
    """
    Get a ChatOpenAI instance configured for a specific node role.

    Resolution order per role:
    - ``triage``: ``llm_triage_model`` → ``llm_model``
    - ``specialist``: ``llm_specialist_model`` → ``llm_model``
    - ``default``: ``llm_model``

    Args:
        role: Which node role this LLM is for.

    Returns:
        Configured ChatOpenAI instance (cached per role).
    """
    if role == "triage":
        model = settings.llm_triage_model or settings.llm_model
        temperature = settings.llm_triage_temperature
        max_tokens = settings.llm_triage_max_tokens
    elif role in ("specialist", "diagnosis"):
        model = settings.llm_specialist_model or settings.llm_model
        temperature = settings.llm_specialist_temperature
        max_tokens = settings.llm_specialist_max_tokens
    else:
        model = settings.llm_model
        temperature = settings.llm_temperature
        max_tokens = settings.llm_max_tokens

    return ChatOpenAI(
        model=model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=temperature,
        max_completion_tokens=max_tokens,
    )


def get_model_name_for_role(role: NodeRole) -> str:
    """Get the model name that *would* be used for a role (without creating LLM)."""
    if role == "triage":
        return settings.llm_triage_model or settings.llm_model
    elif role in ("specialist", "diagnosis"):
        return settings.llm_specialist_model or settings.llm_model
    return settings.llm_model


def clear_llm_cache() -> None:
    """Clear the LRU cache (useful for testing)."""
    get_llm_for_role.cache_clear()
