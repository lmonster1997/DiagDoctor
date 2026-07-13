"""Shim: re-exports from src.evidence for backward compatibility.

This shim will be removed in Phase 7 (cleanup).
"""

from src.evidence.normalizer import ingest

__all__ = ["ingest"]
