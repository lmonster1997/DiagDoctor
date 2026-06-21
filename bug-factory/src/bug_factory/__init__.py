"""Bug Factory — Bug generation and injection for DiagDoctor."""

from bug_factory.ai_rewriter import (
    AIRewriter,
    DiffPatchApplier,
    PatchError,
    RewriteError,
    detect_language,
    extract_code_block,
)
from bug_factory.git_manager import GitManager, GitOperationError
from bug_factory.injector import BugInjector
from bug_factory.schema import (
    BugRecipe,
    Evaluation,
    ExpectedDiagnosis,
    ExpectedObservation,
    Injection,
    InjectionError,
    InjectionResult,
    LogPattern,
    Trigger,
    TriggerStep,
    load_recipe,
    validate_all_recipes,
)

__all__ = [
    "AIRewriter",
    "BugInjector",
    "DiffPatchApplier",
    "GitManager",
    "GitOperationError",
    "InjectionError",
    "InjectionResult",
    "PatchError",
    "RewriteError",
    "detect_language",
    "extract_code_block",
    "BugRecipe",
    "ExpectedDiagnosis",
    "ExpectedObservation",
    "Evaluation",
    "Injection",
    "LogPattern",
    "Trigger",
    "TriggerStep",
    "load_recipe",
    "validate_all_recipes",
]
