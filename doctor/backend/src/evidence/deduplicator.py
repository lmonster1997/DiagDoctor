"""
Deduplicator — collapses repeated log/trace patterns into compact summaries.

Key pattern: N+1 queries produce identical SQL statements repeated N times.
Instead of keeping all N copies (wasting LLM context), fold into one
representative entry with an annotation like "×N".
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Threshold above which repeated patterns are collapsed
_DEDUP_THRESHOLD: int = 3


def _normalize_log_message(msg: str) -> str:
    """
    Normalize a log message to identify semantic duplicates.

    Replaces parameterized values (quoted strings, numbers) with placeholders.
    """
    import re

    # Replace quoted strings
    normalized = re.sub(r"'[^']*'", "?", msg)
    normalized = re.sub(r'"([^"]*)"', "?", normalized)
    # Replace numbers (standalone)
    normalized = re.sub(r"\b\d+(\.\d+)?\b", "#", normalized)
    return normalized.strip()


def dedup_and_fold(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate and fold repeated log patterns.

    For entries with the same normalized message (indicating N+1 or similar loops):
    - Keep the first occurrence with original content
    - Add a count annotation
    - Drop subsequent duplicates

    Args:
        logs: Log entries as dicts (post-denoiser).

    Returns:
        Deduplicated log list with fold annotations.
    """
    if len(logs) <= _DEDUP_THRESHOLD:
        return logs

    # Count normalized patterns
    pattern_counts: Counter[str] = Counter()
    first_occurrence: dict[str, int] = {}

    for i, log in enumerate(logs):
        msg = str(log.get("message", ""))
        normalized = _normalize_log_message(msg)
        pattern_counts[normalized] += 1
        if normalized not in first_occurrence:
            first_occurrence[normalized] = i

    # Build result: keep first occurrence of each pattern, annotate fold count
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    for _i, log in enumerate(logs):
        msg = str(log.get("message", ""))
        normalized = _normalize_log_message(msg)
        count = pattern_counts[normalized]

        if normalized in seen:
            continue

        seen.add(normalized)

        if count >= _DEDUP_THRESHOLD:
            log = dict(log)
            original_msg = log.get("message", "")
            log["message"] = f"[×{count}] {original_msg}"
            log["_fold_count"] = count
            log["_fold_pattern"] = normalized

        result.append(log)

    return result
