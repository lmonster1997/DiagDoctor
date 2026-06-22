"""Benchmark CLI — run evaluation cases against the Doctor agent.

Usage::

    # Run all cases
    uv run python -m benchmark.cli run --suite all

    # Run a single case
    uv run python -m benchmark.cli run --case BE-001

    # Run with custom Doctor API URL
    uv run python -m benchmark.cli run --suite all --doctor-url http://localhost:8001

    # List available cases
    uv run python -m benchmark.cli list-cases

    # Show a specific case
    uv run python -m benchmark.cli show BE-001
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import structlog
from rich.console import Console
from rich.table import Table

from benchmark.evaluators.base import EvaluationScore
from benchmark.evaluators.efficiency import EfficiencyEvaluator
from benchmark.evaluators.exact_match import ExactMatchEvaluator
from benchmark.evaluators.keyword_match import KeywordMatchEvaluator
from benchmark.loader import CaseLoader
from benchmark.runner import BatchRunner
from benchmark.schema import BatchRunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_DOCTOR_URL = "http://localhost:8001"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point for the benchmark CLI."""
    parser = argparse.ArgumentParser(
        description="DiagDoctor Benchmark — evaluate the Doctor agent",
        prog="benchmark",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── `run` command ──────────────────────────────────────────────
    run_parser = subparsers.add_parser("run", help="Run evaluation cases")
    run_group = run_parser.add_mutually_exclusive_group(required=True)
    run_group.add_argument(
        "--suite",
        type=str,
        default=None,
        const="all",
        nargs="?",
        help="Run all cases (or filter by prefix, e.g. 'BE')",
    )
    run_group.add_argument(
        "--case",
        type=str,
        default=None,
        metavar="CASE_ID",
        help="Run a single case by ID (e.g. BE-001)",
    )
    run_parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Filter cases by category (e.g. backend_error)",
    )
    run_parser.add_argument(
        "--doctor-url",
        type=str,
        default=_DEFAULT_DOCTOR_URL,
        help=f"Doctor API base URL (default: {_DEFAULT_DOCTOR_URL})",
    )
    run_parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max concurrent requests to Doctor API (default: 4)",
    )
    run_parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="Skip the LLM Judge evaluator (requires API keys)",
    )

    # ── `list-cases` command ───────────────────────────────────────
    subparsers.add_parser("list-cases", help="List all available evaluation cases")

    # ── `show` command ─────────────────────────────────────────────
    show_parser = subparsers.add_parser("show", help="Show details of a specific case")
    show_parser.add_argument("case_id", type=str, help="Case ID to show (e.g. BE-001)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "list-cases":
        _cmd_list_cases()
    elif args.command == "show":
        _cmd_show(args.case_id)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> None:
    """Execute the ``run`` command."""
    loader = CaseLoader()

    # Determine which cases to run
    cases: list[EvaluationCase]
    if args.case:
        try:
            cases = [loader.load_one(args.case)]
        except FileNotFoundError:
            console.print(f"[red]Error:[/red] Case '{args.case}' not found.")
            sys.exit(1)
    elif args.suite is not None:
        filter_dict: dict[str, Any] | None = None
        if args.category:
            filter_dict = {"category": args.category}

        all_cases = loader.load_suite(filter=filter_dict)

        # If --suite is given with a specific prefix (e.g. "BE"), filter by prefix
        if args.suite and args.suite != "all":
            all_cases = [c for c in all_cases if c.case_id.startswith(args.suite)]

        cases = all_cases
    else:
        console.print("[red]Error:[/red] Must specify --suite or --case")
        sys.exit(1)

    if not cases:
        console.print("[yellow]No cases found.[/yellow]")
        return

    console.print(f"[bold]Running {len(cases)} case(s)...[/bold]\n")

    # ── Run against Doctor API ─────────────────────────────────────
    runner = BatchRunner(
        doctor_api_url=args.doctor_url,
        max_concurrency=args.concurrency,
    )
    batch_result = await runner.run_batch(cases)

    # ── Evaluate ───────────────────────────────────────────────────
    evaluators: list[Any] = [
        ExactMatchEvaluator(),
        KeywordMatchEvaluator(),
        EfficiencyEvaluator(),
    ]

    # LLM Judge requires API keys — only add if not disabled
    if not args.no_llm_judge:
        try:
            from benchmark.evaluators.llm_judge import LLMJudgeEvaluator
            from langchain_openai import ChatOpenAI

            judge_llm = ChatOpenAI(
                model="gpt-4o",
                temperature=0.0,
            )
            evaluators.append(LLMJudgeEvaluator(judge_llm))
            console.print("[dim]LLM Judge evaluator enabled.[/dim]")
        except Exception as exc:
            console.print(f"[yellow]LLM Judge skipped:[/yellow] {exc}")
            console.print(
                "[dim]Set OPENAI_API_KEY env var or use --no-llm-judge to suppress.[/dim]"
            )

    # ── Compute scores ─────────────────────────────────────────────
    all_scores: dict[str, list[EvaluationScore]] = {}
    for result in batch_result.results:
        case = _find_case(cases, result.case_id)
        if case is None:
            continue
        scores: list[EvaluationScore] = []
        for evaluator in evaluators:
            try:
                score = await evaluator.evaluate(case, result)
                scores.append(score)
            except Exception:
                logger.exception(
                    "Evaluator failed",
                    evaluator=evaluator.name,
                    case_id=result.case_id,
                )
                scores.append(
                    EvaluationScore(
                        evaluator=evaluator.name,
                        score=0.0,
                        reasoning=f"Evaluator '{evaluator.name}' raised an exception.",
                    )
                )
        all_scores[result.case_id] = scores

    # ── Print results ──────────────────────────────────────────────
    _print_results_table(batch_result, all_scores)

    # ── Generate reports ───────────────────────────────────────────
    _generate_reports(batch_result, all_scores)


def _generate_reports(
    batch: BatchRunResult,
    scores: dict[str, list[EvaluationScore]],
) -> None:
    """Generate Markdown and HTML reports, saving them to the run directory."""
    from pathlib import Path

    from benchmark.reporters.html import HTMLReporter
    from benchmark.reporters.markdown import MarkdownReporter

    # Determine run directory
    runs_dir = Path(__file__).resolve().parent.parent.parent / "runs" / batch.run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    # ── Markdown report ────────────────────────────────────────────
    try:
        md_reporter = MarkdownReporter()
        md_content = md_reporter.generate(batch, scores)
        md_path = runs_dir / "report.md"
        md_path.write_text(md_content, encoding="utf-8")
        console.print(f"[green]✓[/green] Markdown report saved to [bold]{md_path}[/bold]")
    except Exception:
        logger.exception("Failed to generate Markdown report")
        console.print("[yellow]⚠[/yellow] Markdown report generation failed")

    # ── HTML report ────────────────────────────────────────────────
    try:
        html_reporter = HTMLReporter()
        html_content = html_reporter.generate(batch, scores)
        html_path = runs_dir / "report.html"
        html_path.write_text(html_content, encoding="utf-8")
        console.print(f"[green]✓[/green] HTML dashboard saved to [bold]{html_path}[/bold]")
    except Exception:
        logger.exception("Failed to generate HTML report")
        console.print("[yellow]⚠[/yellow] HTML dashboard generation failed")


def _find_case(cases: list[EvaluationCase], case_id: str) -> EvaluationCase | None:
    """Find a case by ID in a list."""
    for c in cases:
        if c.case_id == case_id:
            return c
    return None


def _print_results_table(
    batch: BatchRunResult,
    all_scores: dict[str, list[EvaluationScore]],
) -> None:
    """Print evaluation results as a rich table."""
    # ── Summary ────────────────────────────────────────────────────
    console.print()
    console.rule("[bold cyan]Evaluation Results[/bold cyan]")

    total = batch.total_cases
    passed = batch.passed
    failed = batch.failed
    pass_rate = passed / total if total > 0 else 0

    console.print(
        f"Total: {total} | Passed: [green]{passed}[/green] | "
        f"Failed: [red]{failed}[/red] | Pass Rate: {pass_rate:.0%}"
    )

    # ── Per-case table ─────────────────────────────────────────────
    table = Table(title="Per-Case Scores")
    table.add_column("Case ID", style="cyan", no_wrap=True)
    table.add_column("Success", justify="center")
    table.add_column("Category", style="magenta")

    # Determine evaluator names from the first case's scores
    evaluator_names: list[str] = []
    if all_scores:
        first_scores = next(iter(all_scores.values()))
        evaluator_names = [s.evaluator for s in first_scores]

    for ename in evaluator_names:
        table.add_column(ename.replace("_", " ").title(), justify="right")

    for result in batch.results:
        scores = all_scores.get(result.case_id, [])
        success_icon = "✅" if result.success else "❌"
        category = result.bug_category or "-"

        row: list[str] = [
            result.case_id,
            success_icon,
            category,
        ]
        for score in scores:
            row.append(f"{score.score:.2f}")
        table.add_row(*row)

    console.print(table)

    # ── Averaged by evaluator ──────────────────────────────────────
    console.print()
    avg_table = Table(title="Average Scores by Evaluator")
    avg_table.add_column("Evaluator", style="cyan")
    avg_table.add_column("Avg Score", justify="right")
    avg_table.add_column("# Cases", justify="right")

    for ename in evaluator_names:
        relevant = [s for scores in all_scores.values() for s in scores if s.evaluator == ename]
        if relevant:
            avg = sum(s.score for s in relevant) / len(relevant)
            avg_table.add_row(ename.replace("_", " ").title(), f"{avg:.2f}", str(len(relevant)))

    console.print(avg_table)

    # ── Detailed reasoning (failed/low-score cases) ────────────────
    console.print()
    console.rule("[bold yellow]Detailed Reasoning[/bold yellow]")
    for result in batch.results:
        scores = all_scores.get(result.case_id, [])
        low_scores = [s for s in scores if s.score < 0.5]
        if low_scores or not result.success:
            console.print(f"\n[bold]{result.case_id}[/bold]:")
            if not result.success:
                console.print(f"  [red]Error:[/red] {result.error}")
            for s in scores:
                style = "green" if s.score >= 0.7 else "yellow" if s.score >= 0.4 else "red"
                console.print(f"  [{style}]{s.evaluator}: {s.score:.2f}[/{style}] — {s.reasoning}")


def _cmd_list_cases() -> None:
    """Execute the ``list-cases`` command."""
    loader = CaseLoader()
    cases = loader.load_suite()

    if not cases:
        console.print("[yellow]No cases found.[/yellow]")
        return

    table = Table(title="Available Evaluation Cases")
    table.add_column("Case ID", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("User Report")

    for case in cases:
        # Truncate long user reports
        report = case.input.user_report
        if len(report) > 80:
            report = report[:80] + "..."
        table.add_row(case.case_id, case.expected.category, report)

    console.print(table)
    console.print(f"\nTotal: [bold]{len(cases)}[/bold] case(s)")


def _cmd_show(case_id: str) -> None:
    """Execute the ``show`` command."""
    loader = CaseLoader()
    try:
        case = loader.load_one(case_id)
    except FileNotFoundError:
        console.print(f"[red]Error:[/red] Case '{case_id}' not found.")
        sys.exit(1)

    console.print(f"[bold cyan]Case: {case.case_id}[/bold cyan]")
    console.print(f"  Recipe ID: {case.recipe_id}")
    console.print(f"  Generated: {case.generated_at}")
    console.print()
    console.print("[bold]Input:[/bold]")
    console.print(f"  User Report: {case.input.user_report}")
    console.print(f"  Trigger Summary: {case.input.trigger_summary}")
    console.print(f"  Evidence: {case.input.evidence}")
    console.print()
    console.print("[bold]Expected:[/bold]")
    console.print(f"  Category: {case.expected.category}")
    console.print(f"  Root Cause: {case.expected.root_cause_summary}")
    console.print(f"  Affected Files: {case.expected.affected_files}")
    console.print(f"  Fix Keywords: {case.expected.fix_keywords}")
    console.print(f"  LLM Judge Criteria: {case.expected.llm_judge_criteria}")


# ---------------------------------------------------------------------------
# Allow running as `python -m benchmark.cli`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
