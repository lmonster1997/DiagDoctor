"""Shim: re-exports from src.engine.agent for backward compatibility.

This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.agent import (
    build_diagnosis_agent,
    clear_diagnosis_agent_cache,
    get_diagnosis_agent,
    load_tools_reference,
    _build_system_prompt,
)

__all__ = [
    "build_diagnosis_agent",
    "clear_diagnosis_agent_cache",
    "get_diagnosis_agent",
    "load_tools_reference",
    "_build_system_prompt",
]
