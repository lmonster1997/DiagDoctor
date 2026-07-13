"""Shim: re-exports from src.evidence.signal_extractor."""
from src.evidence.signal_extractor import extract_golden_signals, _detect_span_n_plus_one

__all__ = ["extract_golden_signals", "_detect_span_n_plus_one"]
