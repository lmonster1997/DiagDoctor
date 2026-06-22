"""HTML dashboard reporter — generates a self-contained interactive evaluation report.

Uses Jinja2 templates with Chart.js (CDN) to produce a rich HTML dashboard
featuring KPI cards, category bar charts, evaluator score charts, an expandable
case-detail table, and an optional pass-rate trend chart derived from historical
run data.

Typical usage::

    from benchmark.schema import BatchRunResult
    from benchmark.reporters.html import HTMLReporter

    reporter = HTMLReporter()
    html = reporter.generate(batch_result, evaluation_scores)
    Path("report.html").write_text(html, encoding="utf-8")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from benchmark.evaluators.base import EvaluationScore
from benchmark.schema import BatchRunResult

# ── Template discovery ────────────────────────────────────────────────
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


def _score_color(score: float) -> str:
    """Return a CSS colour for a score value (green / amber / red)."""
    if score >= 0.7:
        return "#10b981"
    if score >= 0.4:
        return "#f59e0b"
    return "#ef4444"


def _load_historical_runs(runs_dir: Path, max_runs: int = 10) -> list[dict[str, Any]]:
    """Load summary data from historical benchmark runs for trend analysis.

    Args:
        runs_dir: Path to the ``benchmark/runs/`` directory.
        max_runs: Maximum number of historical runs to include.

    Returns:
        A list of ``{"run_id": ..., "pass_rate": ..., "total": ...}`` dicts,
        sorted chronologically.
    """
    if not runs_dir.is_dir():
        return []

    runs: list[dict[str, Any]] = []
    for child in sorted(runs_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        results_file = child / "results.json"
        if not results_file.is_file():
            continue
        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        total = data.get("total_cases", 0)
        passed = data.get("passed", 0)
        runs.append(
            {
                "run_id": child.name,
                "pass_rate": passed / total * 100 if total > 0 else 0.0,
                "total": total,
            }
        )
        if len(runs) >= max_runs:
            break

    # Return in chronological order (oldest first)
    runs.reverse()
    return runs


class HTMLReporter:
    """Generate a self-contained HTML dashboard from a batch run.

    The dashboard is a single HTML file that loads Chart.js from CDN for
    interactive charts.  All data is embedded directly in the page — no
    server-side runtime needed.

    Args:
        title: Optional custom title for the dashboard header.
        runs_dir: Path to ``benchmark/runs/`` for trend data.  When provided
            (and when the directory contains multiple historical runs), a
            pass-rate trend line chart is included.
    """

    def __init__(
        self,
        title: str | None = None,
        runs_dir: str | Path | None = None,
    ) -> None:
        self._title = title or "Evaluation Report"
        if runs_dir is None:
            runs_dir = Path(__file__).resolve().parent.parent.parent.parent / "runs"
        self._runs_dir = Path(runs_dir)

    # ── Public API ──────────────────────────────────────────────────

    def generate(
        self,
        batch_result: BatchRunResult,
        evaluation_scores: dict[str, list[EvaluationScore]] | None = None,
    ) -> str:
        """Generate the complete HTML dashboard.

        Args:
            batch_result: The aggregated batch run result.
            evaluation_scores: Optional per-case evaluation scores, keyed by
                ``case_id``.  When omitted the evaluator chart and score columns
                are suppressed.

        Returns:
            A self-contained HTML string.
        """
        scores = evaluation_scores or {}

        # ── Build template context ─────────────────────────────────
        total = batch_result.total_cases
        passed = batch_result.passed
        failed = batch_result.failed
        pass_rate = passed / total * 100 if total > 0 else 0.0

        latencies = [
            r.metadata.latency_ms
            for r in batch_result.results
            if r.success and r.metadata.latency_ms > 0
        ]
        avg_latency_s = sum(latencies) / len(latencies) / 1000 if latencies else 0.0

        # Evaluator names
        evaluator_names: list[str] = []
        if scores:
            first_scores = next(iter(scores.values()), [])
            evaluator_names = [s.evaluator.replace("_", " ").title() for s in first_scores]

        # ── Category chart data ────────────────────────────────────
        cat_data = self._build_category_chart_data(batch_result)

        # ── Evaluator chart data ───────────────────────────────────
        eval_chart_data = self._build_evaluator_chart_data(scores)

        # ── Evaluator averages ─────────────────────────────────────
        evaluator_avgs = self._build_evaluator_avgs(scores)

        # ── Case rows ──────────────────────────────────────────────
        case_rows = self._build_case_rows(batch_result, scores)

        # ── Trend data ─────────────────────────────────────────────
        trend_labels, trend_values = self._build_trend_data()

        # ── Render template ────────────────────────────────────────
        template = _env.get_template("report.html")
        return template.render(
            title=self._title,
            timestamp=batch_result.timestamp,
            run_id=batch_result.run_id,
            total_cases=total,
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            avg_latency_s=avg_latency_s,
            evaluator_names=evaluator_names,
            evaluator_avgs=evaluator_avgs,
            category_chart_data=cat_data,
            evaluator_chart_data=eval_chart_data,
            case_rows=case_rows,
            trend_labels=trend_labels,
            trend_values=trend_values,
        )

    # ── Context builders ─────────────────────────────────────────────

    @staticmethod
    def _build_category_chart_data(batch: BatchRunResult) -> dict[str, Any]:
        """Build labels and pass-rate arrays for the category bar chart."""
        cat_stats: dict[str, dict[str, float]] = {}
        for r in batch.results:
            cat = r.bug_category or "unknown"
            if cat not in cat_stats:
                cat_stats[cat] = {"total": 0, "passed": 0}
            cs = cat_stats[cat]
            cs["total"] += 1
            if r.success:
                cs["passed"] += 1

        labels: list[str] = []
        pass_rates: list[float] = []
        for cat in sorted(cat_stats.keys()):
            cs = cat_stats[cat]
            labels.append(cat.replace("_", " ").title())
            rate = cs["passed"] / cs["total"] * 100 if cs["total"] > 0 else 0.0
            pass_rates.append(round(rate, 1))

        return {"labels": labels, "pass_rates": pass_rates}

    @staticmethod
    def _build_evaluator_chart_data(
        scores: dict[str, list[EvaluationScore]],
    ) -> dict[str, Any]:
        """Build labels and average-score arrays for the evaluator bar chart."""
        agg: dict[str, list[float]] = {}
        for case_scores in scores.values():
            for s in case_scores:
                agg.setdefault(s.evaluator, []).append(s.score)

        labels: list[str] = []
        avgs: list[float] = []
        for name in sorted(agg.keys()):
            vals = agg[name]
            labels.append(name.replace("_", " ").title())
            avgs.append(round(sum(vals) / len(vals), 3) if vals else 0.0)

        return {"labels": labels, "scores": avgs}

    @staticmethod
    def _build_evaluator_avgs(
        scores: dict[str, list[EvaluationScore]],
    ) -> list[dict[str, Any]]:
        """Build evaluator average summary chips."""
        agg: dict[str, list[float]] = {}
        for case_scores in scores.values():
            for s in case_scores:
                agg.setdefault(s.evaluator, []).append(s.score)

        colors = ["#10b981", "#3b82f6", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4"]
        result: list[dict[str, Any]] = []
        for i, (name, vals) in enumerate(sorted(agg.items())):
            avg = sum(vals) / len(vals) if vals else 0.0
            result.append(
                {
                    "name": name.replace("_", " ").title(),
                    "avg": avg,
                    "count": len(vals),
                    "color": colors[i % len(colors)],
                }
            )
        return result

    def _build_case_rows(
        self,
        batch: BatchRunResult,
        scores: dict[str, list[EvaluationScore]],
    ) -> list[dict[str, Any]]:
        """Build per-case row data for the details table."""
        rows: list[dict[str, Any]] = []
        for r in batch.results:
            case_scores = scores.get(r.case_id, [])
            case_info = self._find_case(batch, r.case_id)

            user_report = ""
            expected: dict[str, Any] = {}
            if case_info:
                user_report = case_info.get("input", {}).get("user_report", "")
                expected = case_info.get("expected", {})

            # Build diagnosis display
            diag = r.diagnosis or {}
            diagnosis_text = json.dumps(diag, indent=2, ensure_ascii=False) if diag else "N/A"

            # Build evaluator reasoning
            reasoning_parts: list[str] = []
            for s in case_scores:
                reasoning_parts.append(f"[{s.evaluator}] score={s.score:.2f}: {s.reasoning}")
            eval_reasoning = "\n".join(reasoning_parts) if reasoning_parts else "N/A"

            # Score values and colors
            score_values = [s.score for s in case_scores]
            score_colors = [_score_color(v) for v in score_values]

            rows.append(
                {
                    "case_id": r.case_id,
                    "success": r.success,
                    "category": r.bug_category or "-",
                    "latency": (
                        f"{r.metadata.latency_ms / 1000:.1f}s" if r.metadata.latency_ms else "N/A"
                    ),
                    "scores": score_values,
                    "score_colors": score_colors,
                    "user_report": user_report,
                    "expected": expected,
                    "diagnosis": diagnosis_text,
                    "eval_reasoning": eval_reasoning,
                }
            )
        return rows

    def _build_trend_data(self) -> tuple[list[str], list[float]]:
        """Build trend labels and values from historical runs.

        Returns:
            A ``(labels, values)`` tuple; both empty if fewer than 2 historical
            runs exist.
        """
        historical = _load_historical_runs(self._runs_dir)
        if len(historical) < 2:
            return [], []

        labels = [r["run_id"] for r in historical]
        values = [r["pass_rate"] for r in historical]
        return labels, values

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _find_case(batch: BatchRunResult, case_id: str) -> dict[str, Any] | None:
        """Find a case dict in *batch.cases* by case_id."""
        for c in batch.cases:
            if c.get("case_id") == case_id:
                return c
        return None


# ── Convenience function ───────────────────────────────────────────────


def generate_html_report(
    batch_result: BatchRunResult,
    evaluation_scores: dict[str, list[EvaluationScore]] | None = None,
) -> str:
    """Convenience wrapper — generate an HTML dashboard in one call."""
    return HTMLReporter().generate(batch_result, evaluation_scores)
