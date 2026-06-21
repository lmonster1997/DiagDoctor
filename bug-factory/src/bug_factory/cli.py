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

    candidates = sorted(
        p for p in search_dir.rglob(f"{prefix}*.yaml") if p.is_file()
    )

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


# ── full (placeholder) ───────────────────────────────────────────────


@cli.command()
@click.argument("recipe_id")
@click.option(
    "--repo",
    "repo_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def full(recipe_id: str, repo_path: Path | None) -> None:
    """Run the full pipeline: inject → trigger → collect → generate case.

    (Trigger, evidence collection, and case generation are not yet implemented.)
    """
    console.print("[bold yellow]⚠ full pipeline not yet implemented[/]")
    console.print("Only the 'inject' step is available. Running injection...\n")

    # Delegate to inject command logic
    ctx = click.get_current_context()
    ctx.invoke(inject, recipe_id=recipe_id, recipe_path=None, repo_path=repo_path)


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
