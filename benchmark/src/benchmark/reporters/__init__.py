"""Benchmark report generators.

Provides :class:`MarkdownReporter` and :class:`HTMLReporter` for generating
human-readable evaluation reports from :class:`~benchmark.schema.BatchRunResult` instances.
"""

from __future__ import annotations

from benchmark.reporters.html import HTMLReporter
from benchmark.reporters.markdown import MarkdownReporter

__all__ = ["HTMLReporter", "MarkdownReporter"]
