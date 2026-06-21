"""Bug Recipe Schema — Pydantic v2 models for bug recipe YAML definition.

Each bug recipe describes:
- What the expected diagnosis should contain
- How to inject the bug into the target codebase
- How to trigger the bug and what should be observed
- How to evaluate whether a Doctor agent diagnosed it correctly
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class ExpectedDiagnosis(BaseModel):
    """The ground-truth diagnosis that a correct Doctor agent should produce."""

    root_cause: str
    affected_file: str
    affected_line: int | None = None
    fix_suggestion: str
    fix_keywords: list[str]  # Keywords that MUST appear in the fix suggestion


class Injection(BaseModel):
    """Describes *how* to inject a bug into the demo-app source code."""

    strategy: Literal["code_replace", "code_insert", "code_delete", "config_change", "env_change"]
    target_file: str  # Path relative to the workspace root
    ai_instruction: str  # Natural-language instruction for the AI rewriter
    diff_patch: str | None = None  # Exact unified-diff patch (alternative to ai_instruction)


class TriggerStep(BaseModel):
    """A single step in a trigger sequence that activates the bug."""

    action: Literal["login", "api_call", "ui_click", "create_data", "wait"]
    params: dict[str, Any]


class LogPattern(BaseModel):
    """Expected log pattern used for observation validation."""

    pattern: str  # Regex pattern
    min_occurrences: int = 1


class ExpectedObservation(BaseModel):
    """Signals expected to appear after a bug is triggered.

    These are used by the evidence collector to verify the bug was activated,
    and by the Doctor agent to locate the root cause.
    """

    log_patterns: list[LogPattern] = []
    trace_attributes: dict[str, dict[str, str]] = {}
    api_response: dict[str, Any] | None = None


class Trigger(BaseModel):
    """Full trigger definition: a sequence of steps that activates the bug."""

    type: Literal["e2e_action", "api_call", "scheduled"]
    steps: list[TriggerStep]
    expected_observation: ExpectedObservation


class Evaluation(BaseModel):
    """Criteria for evaluating the Doctor agent's diagnosis quality."""

    must_mention_keywords: list[str]
    should_mention_keywords: list[str] = []
    llm_judge_criteria: str
    min_confidence: float = 0.6


class BugRecipe(BaseModel):
    """Top-level bug recipe — the complete definition of one injectable bug.

    Recipes are stored as YAML files under ``bug-factory/recipes/``.
    Each file fully describes one bug: what it is, how to inject it,
    how to trigger it, and how to evaluate a Doctor agent's diagnosis.
    """

    id: str = Field(pattern=r"^[A-Z]+-\d{3}$")
    title: str
    category: Literal["frontend_crash", "backend_error", "performance", "logic", "data", "config"]
    severity: Literal["low", "medium", "high", "critical"]
    expected_diagnosis: ExpectedDiagnosis
    injection: Injection
    trigger: Trigger
    evaluation: Evaluation
    tags: list[str] = []


class InjectionResult(BaseModel):
    """The result of successfully injecting a bug into the target repository.

    Returned by :class:`BugInjector.inject` after creating a bug branch,
    modifying the target file(s), and committing the changes.
    """

    recipe_id: str
    branch: str  # e.g. "bug/BE-001"
    diff: str  # Unified diff of all changes against main
    modified_files: list[str]  # Absolute or relative paths of modified files


class InjectionError(Exception):
    """Raised when bug injection fails for any reason.

    Common causes:
    - AI rewriter returned content identical to original
    - Diff patch had no effect
    - File to modify does not exist
    - Git operation failed
    """

    def __init__(self, recipe_id: str, detail: str) -> None:
        self.recipe_id = recipe_id
        self.detail = detail
        super().__init__(f"Injection failed for {recipe_id}: {detail}")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def load_recipe(path: Path | str) -> BugRecipe:
    """Load a single bug recipe from a YAML file.

    Args:
        path: Absolute or relative path to a ``.yaml`` recipe file.

    Returns:
        A validated :class:`BugRecipe` instance.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValidationError: If the YAML content does not conform to the schema.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Recipe file not found: {resolved}")

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"Expected a YAML mapping at top level in {resolved}, got {type(raw).__name__}"
        )

    return BugRecipe.model_validate(raw)


def validate_all_recipes(recipes_dir: Path | str) -> list[ValidationError]:
    """Validate all YAML recipe files under *recipes_dir* (recursively).

    Args:
        recipes_dir: Directory containing ``.yaml`` / ``.yml`` recipe files.

    Returns:
        A (possibly empty) list of :class:`ValidationError` for every recipe
        that fails schema validation.  Recipes that pass are **not** included.
    """
    errors: list[ValidationError] = []
    resolved = Path(recipes_dir).resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))

    for yaml_file in sorted(resolved.rglob("*.yaml")):
        # Skip non-file entries (symlinks pointing to dirs, etc.)
        if not yaml_file.is_file():
            continue
        try:
            load_recipe(yaml_file)
        except ValidationError as exc:
            errors.append(exc)

    for yml_file in sorted(resolved.rglob("*.yml")):
        if not yml_file.is_file():
            continue
        try:
            load_recipe(yml_file)
        except ValidationError as exc:
            errors.append(exc)

    return errors
