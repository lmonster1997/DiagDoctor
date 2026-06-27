"""Benchmark schema — data models for run results and evaluation scores.

Defines :class:`RunResult`, :class:`BatchRunResult`, and related types
used throughout the benchmark harness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from bug_factory.schema import EvaluationCase


class RunMetadata(BaseModel):
    """Per-case metadata collected during a benchmark run."""

    latency_ms: float = 0.0
    doctor_thread_id: str = ""
    findings_count: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    tool_calls: int = 0
    total_cost_usd: float = 0.0
    budget_violated: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """The result of running a single evaluation case against the Doctor agent."""

    case_id: str
    success: bool
    diagnosis: dict[str, Any] | None = None
    bug_category: str | None = None
    metadata: RunMetadata = Field(default_factory=RunMetadata)
    error: str | None = None


class BatchSummary(BaseModel):
    """Aggregated statistics for a batch run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0
    avg_latency_ms: float = 0.0
    total_findings: int = 0
    categories: dict[str, int] = Field(default_factory=dict)


class BatchRunResult(BaseModel):
    """Aggregated result of a complete benchmark batch run."""

    run_id: str
    timestamp: str
    total_cases: int
    passed: int
    failed: int
    results: list[RunResult] = Field(default_factory=list)
    summary: BatchSummary = Field(default_factory=BatchSummary)
    cases: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_results(
        cls,
        run_id: str,
        results: list[RunResult],
        cases: list[EvaluationCase] | None = None,
    ) -> BatchRunResult:
        """Build a BatchRunResult from a list of RunResult instances."""
        total = len(results)
        passed = sum(1 for r in results if r.success)
        failed = total - passed
        latencies = [r.metadata.latency_ms for r in results if r.success]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        total_findings = sum(r.metadata.findings_count for r in results if r.success)

        categories: dict[str, int] = {}
        for r in results:
            if r.bug_category:
                categories[r.bug_category] = categories.get(r.bug_category, 0) + 1

        return cls(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),  # noqa: UP017
            total_cases=total,
            passed=passed,
            failed=failed,
            results=results,
            summary=BatchSummary(
                total=total,
                passed=passed,
                failed=failed,
                pass_rate=passed / total if total > 0 else 0.0,
                avg_latency_ms=avg_latency,
                total_findings=total_findings,
                categories=categories,
            ),
            cases=[c.model_dump() for c in cases] if cases else [],
        )
