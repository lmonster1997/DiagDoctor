"""Markdown report generator — produces a human-readable evaluation report.

Generates a structured Markdown document from a :class:`~benchmark.schema.BatchRunResult`,
including a summary table, per-category breakdown, per-evaluator averages (when scores
are supplied), detailed failure analysis, and token-usage statistics.

Typical usage::

    from benchmark.schema import BatchRunResult
    from benchmark.reporters.markdown import MarkdownReporter

    reporter = MarkdownReporter()
    md = reporter.generate(batch_result, evaluation_scores)
    Path("report.md").write_text(md, encoding="utf-8")
"""

from __future__ import annotations

from typing import Any

from benchmark.evaluators.base import EvaluationScore
from benchmark.schema import BatchRunResult, RunResult


class MarkdownReporter:
    """Generate a Markdown evaluation report from a batch run.

    The report includes:

    * **Summary** — total / passed / failed / pass-rate / avg latency.
    * **By Category** — pass rate and average latency per bug category.
    * **By Evaluator** — average score per evaluator (requires *evaluation_scores*).
    * **Failed Cases Details** — user report, expected vs actual diagnosis, evaluator
      reasoning for each failed or low-scoring case.
    * **Token Usage** — aggregated token consumption across all cases.

    Args:
        title: Optional custom title for the report header.
    """

    def __init__(self, title: str | None = None) -> None:
        self._title = title

    # ── Public API ──────────────────────────────────────────────────

    def generate(
        self,
        batch_result: BatchRunResult,
        evaluation_scores: dict[str, list[EvaluationScore]] | None = None,
    ) -> str:
        """Generate a complete Markdown report string.

        Args:
            batch_result: The aggregated batch run result.
            evaluation_scores: Optional per-case evaluation scores, keyed by
                ``case_id``.  When omitted the *By Evaluator* section and
                detailed score reasoning are suppressed.

        Returns:
            A Markdown-formatted string ready to write to disk.
        """
        lines: list[str] = []

        # ── Header ─────────────────────────────────────────────────
        title = self._title or "Evaluation Report"
        lines.append(f"# {title} — {batch_result.timestamp}")
        lines.append("")

        # ── Summary ────────────────────────────────────────────────
        lines.extend(self._render_summary(batch_result))

        # ── By Category ────────────────────────────────────────────
        lines.extend(self._render_by_category(batch_result))

        # ── By Evaluator ───────────────────────────────────────────
        if evaluation_scores:
            lines.extend(self._render_by_evaluator(evaluation_scores))

        # ── Per-Case Details ───────────────────────────────────────
        lines.extend(self._render_case_details(batch_result, evaluation_scores or {}))

        # ── Failed Cases ───────────────────────────────────────────
        lines.extend(self._render_failed_cases(batch_result, evaluation_scores or {}))

        # ── Token Usage ────────────────────────────────────────────
        lines.extend(self._render_token_usage(batch_result))

        return "\n".join(lines)

    # ── Section renderers ───────────────────────────────────────────

    @staticmethod
    def _render_summary(batch: BatchRunResult) -> list[str]:
        """Render the summary section."""
        total = batch.total_cases
        passed = batch.passed
        failed = batch.failed
        pass_rate = passed / total * 100 if total > 0 else 0.0

        latencies = [
            r.metadata.latency_ms for r in batch.results if r.success and r.metadata.latency_ms > 0
        ]
        avg_latency_s = sum(latencies) / len(latencies) / 1000 if latencies else 0.0

        lines: list[str] = [
            "## Summary",
            "",
            f"- **Total Cases**: {total}",
            f"- **Passed**: {passed} ({pass_rate:.0f}%)",
            f"- **Failed**: {failed}",
            f"- **Avg Latency**: {avg_latency_s:.1f}s",
            "",
        ]
        return lines

    @staticmethod
    def _render_by_category(batch: BatchRunResult) -> list[str]:
        """Render the per-category breakdown table."""
        categories = batch.summary.categories
        if not categories:
            return []

        # Build per-category stats
        cat_stats: dict[str, dict[str, Any]] = {}
        for r in batch.results:
            cat = r.bug_category or "unknown"
            if cat not in cat_stats:
                cat_stats[cat] = {"total": 0, "passed": 0, "latencies": []}
            cs = cat_stats[cat]
            cs["total"] += 1
            if r.success:
                cs["passed"] += 1
                if r.metadata.latency_ms > 0:
                    cs["latencies"].append(r.metadata.latency_ms)

        lines: list[str] = [
            "## By Category",
            "",
            "| Category | Total | Pass Rate | Avg Latency |",
            "|----------|-------|-----------|-------------|",
        ]

        for cat, stats in sorted(cat_stats.items()):
            total = stats["total"]
            pct = stats["passed"] / total * 100 if total > 0 else 0.0
            lats = stats["latencies"]
            avg_lat = f"{sum(lats) / len(lats) / 1000:.1f}s" if lats else "N/A"
            lines.append(f"| {cat} | {total} | {pct:.0f}% | {avg_lat} |")

        lines.append("")
        return lines

    @staticmethod
    def _render_by_evaluator(scores: dict[str, list[EvaluationScore]]) -> list[str]:
        """Render average scores per evaluator."""
        # Aggregate by evaluator name
        agg: dict[str, list[float]] = {}
        for case_scores in scores.values():
            for s in case_scores:
                agg.setdefault(s.evaluator, []).append(s.score)

        if not agg:
            return []

        lines: list[str] = [
            "## By Evaluator",
            "",
            "| Evaluator | Avg Score | # Cases |",
            "|-----------|-----------|---------|",
        ]

        for name, vals in sorted(agg.items()):
            avg = sum(vals) / len(vals) if vals else 0.0
            display = name.replace("_", " ").title()
            lines.append(f"| {display} | {avg:.2f} | {len(vals)} |")

        lines.append("")
        return lines

    @staticmethod
    def _render_case_details(
        batch: BatchRunResult,
        scores: dict[str, list[EvaluationScore]],
    ) -> list[str]:
        """Render a per-case summary table."""
        if not batch.results:
            return []

        lines: list[str] = [
            "## Case Details",
            "",
        ]

        # Determine evaluator names from scores
        eval_names: list[str] = []
        if scores:
            first = next(iter(scores.values()), [])
            eval_names = [s.evaluator for s in first]

        # Build header
        header_cols = ["Case ID", "Status", "Category", "Latency"]
        for en in eval_names:
            header_cols.append(en.replace("_", " ").title())
        sep = "| " + " | ".join(header_cols) + " |"
        sep2 = "|" + "|".join("---" for _ in header_cols) + "|"
        lines.append(sep)
        lines.append(sep2)

        for r in batch.results:
            status = "✅ Pass" if r.success else "❌ Fail"
            cat = r.bug_category or "-"
            lat = f"{r.metadata.latency_ms / 1000:.1f}s" if r.metadata.latency_ms else "N/A"

            row = [r.case_id, status, cat, lat]
            for s in scores.get(r.case_id, []):
                row.append(f"{s.score:.2f}")
            lines.append("| " + " | ".join(row) + " |")

        lines.append("")
        return lines

    @staticmethod
    def _render_failed_cases(
        batch: BatchRunResult,
        scores: dict[str, list[EvaluationScore]],
    ) -> list[str]:
        """Render detailed info for failed or low-scoring cases."""
        # Identify problematic cases: failed or any evaluator score < 0.5
        problematic: list[tuple[RunResult, float]] = []
        for r in batch.results:
            case_scores = scores.get(r.case_id, [])
            min_score = min((s.score for s in case_scores), default=1.0)
            if not r.success or min_score < 0.5:
                problematic.append((r, min_score))

        if not problematic:
            return []

        lines: list[str] = [
            "## Failed / Low-Scoring Cases",
            "",
        ]

        for r, _min_score in problematic:
            lines.append(f"### {r.case_id}")
            lines.append("")

            # Find the matching case from batch.cases for expected info
            case_info = MarkdownReporter._find_case(batch, r.case_id)

            if not r.success:
                lines.append(f"- **Error**: {r.error or 'Unknown error'}")
            else:
                diag = r.diagnosis or {}
                lines.append(f"- **Category**: {diag.get('bug_category', 'N/A')}")
                lines.append(f"- **Root Cause**: {diag.get('root_cause', 'N/A')}")
                lines.append(f"- **Affected File**: {diag.get('affected_file', 'N/A')}")
                lines.append(f"- **Fix Suggestion**: {diag.get('fix_suggestion', 'N/A')}")
                lines.append(f"- **Confidence**: {diag.get('confidence', 'N/A')}")

            if case_info:
                expected = case_info.get("expected", {})
                lines.append(f"- **Expected Category**: {expected.get('category', 'N/A')}")
                lines.append(
                    f"- **Expected Root Cause**: {expected.get('root_cause_summary', 'N/A')}"
                )
                lines.append(
                    f"- **Expected Files**: {', '.join(expected.get('affected_files', [])) or 'N/A'}"
                )
                lines.append(
                    f"- **Expected Keywords**: {', '.join(expected.get('fix_keywords', [])) or 'N/A'}"
                )

            # Evaluator reasoning
            case_scores = scores.get(r.case_id, [])
            if case_scores:
                lines.append("")
                lines.append("**Evaluator Scores:**")
                lines.append("")
                for s in case_scores:
                    lines.append(f"- **{s.evaluator}**: {s.score:.2f} — {s.reasoning}")

            lines.append("")

        return lines

    @staticmethod
    def _render_token_usage(batch: BatchRunResult) -> list[str]:
        """Render aggregated token usage statistics."""
        total_tokens: dict[str, int] = {}
        for r in batch.results:
            for model, count in r.metadata.token_usage.items():
                total_tokens[model] = total_tokens.get(model, 0) + count

        lines: list[str] = [
            "## Token Usage",
            "",
        ]

        if not total_tokens:
            lines.append("_No token usage data available._")
            lines.append("")
            return lines

        lines.append("| Model | Total Tokens |")
        lines.append("|-------|-------------|")

        grand_total = 0
        for model, count in sorted(total_tokens.items()):
            lines.append(f"| {model} | {count:,} |")
            grand_total += count

        lines.append(f"| **Total** | **{grand_total:,}** |")
        lines.append("")

        return lines

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _find_case(batch: BatchRunResult, case_id: str) -> dict[str, Any] | None:
        """Find a case dict in *batch.cases* by case_id."""
        for c in batch.cases:
            if c.get("case_id") == case_id:
                return c
        return None


# ── Convenience function ───────────────────────────────────────────────


def generate_markdown_report(
    batch_result: BatchRunResult,
    evaluation_scores: dict[str, list[EvaluationScore]] | None = None,
) -> str:
    """Convenience wrapper — generate a Markdown report in one call."""
    return MarkdownReporter().generate(batch_result, evaluation_scores)
