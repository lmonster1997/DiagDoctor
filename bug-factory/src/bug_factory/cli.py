"""
Bug Factory CLI — command-line interface for bug injection workflow.

Provides commands for validating recipes, injecting bugs, and running
the full bug-generation pipeline.

Environment variables are automatically loaded from ``.env`` files
(searched in: workspace root, doctor/, bug-factory/).

Usage::

    # Validate all recipes
    python -m bug_factory.cli validate

    # Inject a single bug
    python -m bug_factory.cli inject BE-001

    # Inject a bug from a custom recipe path
    python -m bug_factory.cli inject BE-001 --recipe recipes/be_001_n_plus_1.yaml

    # Full pipeline (inject → trigger → collect → generate case)
    python -m bug_factory.cli full BE-001
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

    from bug_factory.schema import TriggerResult

from bug_factory.schema import InjectionResult, load_recipe, validate_all_recipes

console = Console()

# ── Constants ────────────────────────────────────────────────────────

# Workspace root: bug-factory/src/bug_factory/cli.py → go up 3 levels
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RECIPES_DIR = _WORKSPACE_ROOT / "bug-factory" / "recipes"

# ── .env auto-loading ────────────────────────────────────────────────


def _load_dotenv_files() -> None:
    """Search for and load ``.env`` files in priority order.

    Searches (first wins for each key):
    1. ``{workspace}/.env``
    2. ``{workspace}/doctor/.env``
    3. ``{workspace}/bug-factory/.env``

    Existing environment variables are **never** overwritten.
    """
    candidates = [
        _WORKSPACE_ROOT / ".env",
        _WORKSPACE_ROOT / "doctor" / ".env",
        _WORKSPACE_ROOT / "bug-factory" / ".env",
    ]
    loaded = 0
    for env_path in candidates:
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            loaded += 1
            console.print(f"[dim]Loaded env: {env_path}[/]")
    if loaded > 0:
        console.print(f"[dim]LLM model: {os.getenv('LLM_MODEL', 'gpt-4o')}[/]")


_load_dotenv_files()


# ── Helpers ──────────────────────────────────────────────────────────


def _find_recipe(recipe_id: str, recipes_dir: Path | None = None) -> Path:
    """Find a recipe YAML file by its ID.

    Searches *recipes_dir* (default ``bug-factory/recipes/``) for a file
    whose name starts with the lowercased *recipe_id*.

    Args:
        recipe_id: Recipe identifier (e.g. ``"BE-001"``).
        recipes_dir: Optional override directory to search.

    Returns:
        Path to the matching YAML file.

    Raises:
        click.ClickException: If zero or multiple matches found.
    """
    search_dir = Path(recipes_dir).resolve() if recipes_dir else _RECIPES_DIR
    prefix = recipe_id.lower().replace("-", "_")

    candidates = sorted(p for p in search_dir.rglob(f"{prefix}*.yaml") if p.is_file())

    if not candidates:
        raise click.ClickException(
            f"No recipe found for ID '{recipe_id}' in {search_dir}. "
            f"Expected filename prefix: {prefix}"
        )
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise click.ClickException(
            f"Multiple recipes match '{recipe_id}': {names}. "
            "Use --recipe to specify the exact file."
        )

    return candidates[0]


def _get_llm() -> ChatOpenAI:
    """Create an LLM instance from environment variables.

    Uses the standard OpenAI-compatible environment variables:
    ``OPENAI_API_KEY``, ``OPENAI_BASE_URL``, ``LLM_MODEL``.

    Returns:
        A LangChain ``ChatOpenAI`` instance.

    Raises:
        click.ClickException: If ``OPENAI_API_KEY`` is not set.
    """
    import os

    from langchain_openai import ChatOpenAI

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise click.ClickException(
            "OPENAI_API_KEY or LLM_API_KEY environment variable is required. "
            "Set it before running: $env:OPENAI_API_KEY='...'"
        )

    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL", "gpt-4o")

    kwargs: dict[str, object] = {
        "model": model,
        "openai_api_key": api_key,
        "temperature": 0.2,
    }
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)  # type: ignore[arg-type]


# ── CLI group ────────────────────────────────────────────────────────


@click.group()
@click.version_option(version="0.1.0", prog_name="bug-factory")
def cli() -> None:
    """Bug Factory — Generate and inject bugs into the DiagDoctor demo-app.

    Manage bug recipes, inject them into the target codebase, and
    run the full bug-generation pipeline.
    """


# ── validate ─────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--dir",
    "recipes_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing recipe YAML files (default: bug-factory/recipes/)",
)
def validate(recipes_dir: Path | None) -> None:
    """Validate all bug recipe YAML files."""
    search_dir = recipes_dir or _RECIPES_DIR
    console.print(f"[bold]Validating recipes in[/] {search_dir}")

    errors = validate_all_recipes(search_dir)

    if errors:
        console.print(f"\n[bold red]✗ {len(errors)} validation error(s)[/]\n")
        for i, err in enumerate(errors, 1):
            console.print(f"[red]Error {i}:[/] {err}")
        raise SystemExit(1)
    else:
        # Count recipes
        count = len(list(search_dir.rglob("*.yaml")))
        console.print(f"\n[bold green]✓ All {count} recipes valid[/]")


# ── inject ───────────────────────────────────────────────────────────


@cli.command()
@click.argument("recipe_id")
@click.option(
    "--recipe",
    "recipe_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a specific recipe YAML file (auto-discovered by default)",
)
@click.option(
    "--repo",
    "repo_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the target git repository (default: workspace root)",
)
def inject(recipe_id: str, recipe_path: Path | None, repo_path: Path | None) -> None:
    """Inject a bug recipe into the target repository.

    RECIPE_ID: The bug recipe identifier (e.g. BE-001, FE-001).
    """
    from bug_factory.injector import BugInjector

    # Resolve recipe
    yaml_path = recipe_path or _find_recipe(recipe_id)
    console.print(f"[bold]Loading recipe:[/] {yaml_path.name}")
    recipe = load_recipe(yaml_path)

    # Resolve repo
    repo = repo_path.resolve() if repo_path else _WORKSPACE_ROOT
    console.print(f"[bold]Target repository:[/] {repo}")

    # LLM
    llm = _get_llm()
    console.print(f"[dim]LLM model: {llm.model_name}[/]")

    # Inject
    injector = BugInjector(repo_path=repo, llm=llm)

    async def _run() -> InjectionResult:
        return await injector.inject(recipe)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        console.print(f"\n[bold red]✗ Injection failed:[/] {exc}")
        raise SystemExit(1) from exc

    # Display result
    _display_injection_result(result)


def _display_injection_result(result: InjectionResult) -> None:
    """Pretty-print an injection result to the console."""
    table = Table(title="Injection Result", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Recipe ID", result.recipe_id)
    table.add_row("Branch", result.branch)
    table.add_row("Files Modified", ", ".join(result.modified_files))
    diff_preview = result.diff[:500] + ("..." if len(result.diff) > 500 else "")
    table.add_row("Diff (preview)", diff_preview)
    console.print(table)
    console.print("\n[bold green]✓ Injection complete![/]")


# ── trigger ──────────────────────────────────────────────────────────


@cli.command()
@click.argument("recipe_id")
@click.option(
    "--recipe",
    "recipe_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a specific recipe YAML file (auto-discovered by default)",
)
@click.option(
    "--base-url",
    default="http://localhost:8000",
    show_default=True,
    help="Base URL of the running demo-app backend",
)
@click.option(
    "--no-ui/--ui",
    default=False,
    show_default=True,
    help="Skip UI click actions (useful when Playwright is not installed)",
)
def trigger_cmd(
    recipe_id: str,
    recipe_path: Path | None,
    base_url: str,
    no_ui: bool,  # noqa: FBT001
) -> None:
    """Execute the trigger sequence from a bug recipe against the demo-app.

    RECIPE_ID: The bug recipe identifier (e.g. BE-001, FE-001).

    Requires the demo-app backend to be running at --base-url.

    \b
    Examples:
        python -m bug_factory.cli trigger BE-001
        python -m bug_factory.cli trigger BE-001 --base-url http://localhost:8000
        python -m bug_factory.cli trigger FE-001 --no-ui
    """
    from bug_factory.trigger import TriggerRunner

    # Resolve recipe
    yaml_path = recipe_path or _find_recipe(recipe_id)
    console.print(f"[bold]Loading recipe:[/] {yaml_path.name}")
    recipe = load_recipe(yaml_path)

    console.print(f"[bold]Trigger type:[/] {recipe.trigger.type}")
    console.print(f"[bold]Steps:[/] {len(recipe.trigger.steps)}")
    console.print(f"[bold]Base URL:[/] {base_url}")

    if no_ui:
        console.print("[dim]UI actions will be skipped (--no-ui)[/]")

    runner = TriggerRunner(demo_app_base_url=base_url)

    async def _run() -> TriggerResult:
        return await runner.run(recipe.trigger)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        console.print(f"\n[bold red]✗ Trigger execution failed:[/] {exc}")
        raise SystemExit(1) from exc

    _display_trigger_result(result)


def _display_trigger_result(result: TriggerResult) -> None:
    """Pretty-print a trigger result to the console."""
    if result.success:
        console.print("\n[bold green]✓ Trigger completed successfully![/]")
    else:
        console.print(f"\n[bold red]✗ Trigger failed:[/] {result.error}")

    table = Table(title="Trigger Steps", show_header=True)
    table.add_column("#", style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Success", style="white")
    table.add_column("Elapsed (ms)", style="magenta", justify="right")
    table.add_column("Response / Error", style="white")

    for i, step in enumerate(result.steps):
        status_icon = "[green]✓[/]" if step.success else "[red]✗[/]"
        detail = ""
        if step.success:
            if step.response:
                # Show a compact summary of the response.
                detail = str(step.response)
                if len(detail) > 120:
                    detail = detail[:120] + "..."
        else:
            detail = f"[red]{step.error or 'unknown'}[/]"
        table.add_row(str(i + 1), step.action, status_icon, f"{step.elapsed_ms:.1f}", detail)

    console.print(table)

    # Show session summary
    console.print("\n[bold]Session State:[/]")
    console.print(f"  Token: {'[green]set[/]' if result.session.get('token') else '[dim]none[/]'}")
    projects = result.session.get("created_projects", [])
    tasks = result.session.get("created_tasks", [])
    console.print(f"  Created Projects: {len(projects)}")
    console.print(f"  Created Tasks: {len(tasks)}")


# ── full ─────────────────────────────────────────────────────────────


@cli.command()
@click.argument("recipe_id")
@click.option(
    "--repo",
    "repo_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--base-url",
    default="http://localhost:8000",
    show_default=True,
    help="Base URL of the running demo-app backend (for trigger step)",
)
@click.option(
    "--skip-trigger",
    is_flag=True,
    default=False,
    help="Only inject the bug; do not run the trigger sequence",
)
@click.option(
    "--no-ui/--ui",
    default=False,
    show_default=True,
    help="Skip UI click actions during trigger",
)
def full(
    recipe_id: str,
    repo_path: Path | None,
    base_url: str,
    skip_trigger: bool,  # noqa: FBT001
    no_ui: bool,  # noqa: FBT001
) -> None:
    """Run the full pipeline: inject → trigger → collect → generate case.

    RECIPE_ID: The bug recipe identifier (e.g. BE-001, FE-001).

    \b
    Steps:
        1. Inject the bug into the target repository (creates bug/ branch).
        2. Trigger the bug against the running demo-app.
        3. (collect & case generation coming soon)

    \b
    Note: After injection you must rebuild & redeploy the demo-app for the
    injected bug to take effect (e.g. `docker compose up -d --build demo-backend`).
    """
    from bug_factory.injector import BugInjector
    from bug_factory.trigger import TriggerRunner

    # Resolve recipe
    yaml_path = _find_recipe(recipe_id)
    console.print(f"[bold]Loading recipe:[/] {yaml_path.name}")
    recipe = load_recipe(yaml_path)

    # Step 1: Inject
    console.print("\n[bold]── Step 1: Inject ──[/]")
    repo = repo_path.resolve() if repo_path else _WORKSPACE_ROOT
    llm = _get_llm()
    injector = BugInjector(repo_path=repo, llm=llm)

    async def _inject() -> InjectionResult:
        return await injector.inject(recipe)

    try:
        injection_result = asyncio.run(_inject())
    except Exception as exc:
        console.print(f"[bold red]✗ Injection failed:[/] {exc}")
        raise SystemExit(1) from exc

    _display_injection_result(injection_result)

    if skip_trigger:
        console.print("\n[dim]Skipping trigger step (--skip-trigger)[/]")
        return

    # Step 2: Trigger
    console.print("\n[bold]── Step 2: Trigger ──[/]")
    console.print(
        "[yellow]⚠ Make sure you have rebuilt & redeployed the demo-app "
        "with the injected bug before continuing![/]"
    )

    runner = TriggerRunner(demo_app_base_url=base_url)

    async def _trigger() -> TriggerResult:
        return await runner.run(recipe.trigger)

    try:
        trigger_result = asyncio.run(_trigger())
    except Exception as exc:
        console.print(f"[bold red]✗ Trigger failed:[/] {exc}")
        raise SystemExit(1) from exc

    _display_trigger_result(trigger_result)

    # Step 3: Collect & generate (coming soon)
    console.print("\n[dim]Evidence collection & case generation coming soon...[/]")

    if trigger_result.success:
        console.print("\n[bold green]✓ Full pipeline (inject + trigger) complete![/]")
    else:
        console.print("\n[bold yellow]⚠ Injection succeeded but trigger had failures.[/]")


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
