"""
Denoiser — strips high-frequency noise from logs while protecting tier-specific signals.

Rules:
- Backend: drop /health, http_request info-level lines (unless otherwise interesting)
- Frontend: KEEP sparse logs — a frontend crash may have only 1-2 log lines
- Both: preserve any ERROR/WARNING level
"""

from __future__ import annotations

from typing import Any

# Log patterns to filter as noise (case-insensitive substring match).
# These are sensible defaults for any HTTP service; extend via env
# DENOISER_EXTRA_NOISE_PATTERNS if your app has specific noise endpoints.
_BACKEND_NOISE_PATTERNS: list[str] = [
    "/health",
    "/metrics",
    "GET /health",
]

# Log level priorities for noise determination
_NOISE_LEVELS: frozenset[str] = frozenset({"INFO", "DEBUG"})


def _is_backend_noise(log: dict[str, Any]) -> bool:
    """Check if a log entry is backend noise."""
    msg = str(log.get("message", "")).lower()
    level = str(log.get("level", "INFO")).upper()

    # Always keep ERROR/WARNING
    if level not in _NOISE_LEVELS:
        return False

    # Check against known noise patterns
    for pattern in _BACKEND_NOISE_PATTERNS:
        if pattern.lower() in msg:
            return True

    # Keep http_request info if it contains error-related attributes
    if "http_request" in msg or "HTTP" in msg.upper():
        attrs = log.get("attributes", {})
        status = attrs.get("http.status_code", attrs.get("status_code", 0))
        return not (isinstance(status, (int, float)) and status >= 400)

    return False


def denoise_logs(
    logs: list[dict[str, Any]],
    protect_tier: str = "frontend",
) -> list[dict[str, Any]]:
    """
    Remove noise from log entries.

    Backend: aggressively strip /health and info-level http_request lines.
    Frontend: keep all entries (sparse by nature, crashes produce few lines).

    Args:
        logs: Raw log entries as dicts.
        protect_tier: If "frontend", preserve all frontend logs regardless of level.

    Returns:
        Filtered list of log entries.
    """
    preserved: list[dict[str, Any]] = []

    for log in logs:
        # Check top-level first, then labels.service_name (Loki format)
        svc_raw = str(log.get("service_name", log.get("service", "")))
        if not svc_raw:
            labels = log.get("labels")
            if isinstance(labels, dict):
                svc_raw = str(labels.get("service_name", labels.get("service", "")))
        service_name = svc_raw.lower()

        # Always protect frontend logs (sparse, critical for debugging)
        is_frontend = "frontend" in service_name or service_name.startswith("demo-frontend")
        if protect_tier == "frontend" and is_frontend:
            preserved.append(log)
            continue

        # Filter backend noise
        if is_frontend:
            preserved.append(log)  # frontend logs are never filtered
        elif not _is_backend_noise(log):
            preserved.append(log)

    return preserved


def compute_noise_ratio(
    raw_logs: list[dict[str, Any]],
    denoised_logs: list[dict[str, Any]],
) -> float:
    """
    Compute the noise ratio of the raw evidence.

    Returns 0.0 if no raw logs, otherwise (raw - denoised) / raw.
    """
    raw_count = len(raw_logs)
    if raw_count == 0:
        return 0.0
    return max(0.0, min(1.0, (raw_count - len(denoised_logs)) / raw_count))
