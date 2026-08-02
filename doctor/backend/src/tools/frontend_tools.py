"""Frontend error parsing helpers shared by the inspect_frontend_error tool.

The standalone ``parse_browser_errors`` / ``extract_stack_trace`` tools and
their StructuredTool wrappers were removed in the V3 slim-down -- the active
``INSPECT_FRONTEND_ERROR_TOOL`` (``frontend_inspect.py``) merges that logic
inline and only reuses the two pure helpers below.
"""

from __future__ import annotations

import re


def _classify_error_type(message: str, stack: str) -> str:
    """Classify a browser error by its message and stack content."""
    combined = (message + " " + stack).lower()

    if "typeerror" in combined:
        if "undefined" in combined:
            return "TypeError(undefined_access)"
        if "null" in combined:
            return "TypeError(null_access)"
        if "not a function" in combined:
            return "TypeError(not_a_function)"
        return "TypeError"

    if "referenceerror" in combined:
        return "ReferenceError"
    if "syntaxerror" in combined:
        return "SyntaxError"
    if "rangeerror" in combined:
        return "RangeError"
    if "networkerror" in combined or "failed to fetch" in combined:
        return "NetworkError"
    if "chunkloaderror" in combined or "loading chunk" in combined:
        return "ChunkLoadError"
    if "unhandled rejection" in combined or "promise" in combined:
        return "PromiseRejection"
    if "react" in combined and "error" in combined:
        return "ReactRenderError"

    return "UnknownError"


def _extract_component_name(text: str) -> str:
    """Extract React component name from error message or component stack."""
    # Pattern: "at ComponentName (http://...)"
    pattern = r"at\s+(\w+)\s*\("
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    # Fallback: look for component_stack field
    pattern2 = r"at\s+(\w+)\s+\(http"
    match2 = re.search(pattern2, text)
    if match2:
        return match2.group(1)
    return "unknown"


__all__ = [
    "_classify_error_type",
    "_extract_component_name",
]
