"""
Bug Injector — orchestrates the injection of a bug into the target repository.

The :class:`BugInjector` is the central orchestrator for applying a
:class:`BugRecipe` to a live codebase.  It delegates to:

- :class:`GitManager` for branch management and version control.
- :class:`AIRewriter` for AI-driven code modification.
- :class:`DiffPatchApplier` for precise diff-patch application.

Usage::

    from langchain_openai import ChatOpenAI
    from pathlib import Path
    from bug_factory.injector import BugInjector
    from bug_factory.schema import load_recipe

    llm = ChatOpenAI(model="gpt-4o")
    injector = BugInjector(repo_path=Path("../.."), llm=llm)
    recipe = load_recipe("recipes/be_001_n_plus_1.yaml")
    result = await injector.inject(recipe)
    print(result.branch)  # → "bug/BE-001"
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from bug_factory.ai_rewriter import AIRewriter, DiffPatchApplier, detect_language
from bug_factory.git_manager import GitManager
from bug_factory.schema import InjectionError, InjectionResult

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from bug_factory.schema import BugRecipe

logger = structlog.get_logger(__name__)


class BugInjector:
    """Orchestrates the full bug-injection pipeline.

    1. Creates a ``bug/{recipe_id}`` branch from ``main``.
    2. Modifies the target file(s) via AI rewriting or diff patch.
    3. Validates that the file was actually changed.
    4. Commits the changes.
    5. Returns a summary :class:`InjectionResult`.
    """

    def __init__(self, repo_path: Path, llm: BaseChatModel) -> None:
        """Initialise the injector.

        Args:
            repo_path: Absolute or relative path to the git repository
                root (the directory containing ``.git``).  This is
                typically the DiagDoctor workspace root.
            llm: A LangChain chat model instance used by the AI rewriter.
        """
        self.repo_path = repo_path.resolve()
        self.git = GitManager(self.repo_path)
        self.rewriter = AIRewriter(llm)
        self.patch_applier = DiffPatchApplier()
        logger.info(
            "BugInjector initialised",
            repo_path=str(self.repo_path),
        )

    async def inject(self, recipe: BugRecipe) -> InjectionResult:
        """Inject a bug into the target repository according to *recipe*.

        Args:
            recipe: A validated :class:`BugRecipe` describing what to inject
                and where.

        Returns:
            An :class:`InjectionResult` summarising the branch, diff, and
            modified files.

        Raises:
            InjectionError: If the target file does not exist, the AI rewriter
                produces no changes, or a Git operation fails.
        """
        logger.info(
            "Starting injection",
            recipe_id=recipe.id,
            title=recipe.title,
            strategy=recipe.injection.strategy,
            target_file=recipe.injection.target_file,
        )

        # ── 1. Create bug branch ──────────────────────────────────
        branch = self.git.create_bug_branch(recipe.id)
        logger.info("Bug branch ready", branch=branch)

        # ── 2. Resolve target file ─────────────────────────────────
        target = self.repo_path / recipe.injection.target_file
        if not target.is_file():
            raise InjectionError(
                recipe.id,
                f"Target file does not exist: {target}",
            )

        original = target.read_text(encoding="utf-8")
        logger.debug(
            "Read original file",
            path=str(target),
            lines=len(original.splitlines()),
        )

        # ── 3. Apply injection to primary target ───────────────────
        if recipe.injection.diff_patch:
            modified = self._apply_diff_patch(recipe.id, original, recipe.injection.diff_patch)
        else:
            lang = detect_language(target)
            modified = await self._apply_ai_rewrite(
                recipe.id,
                original,
                recipe.injection.ai_instruction,
                lang,
            )

        # ── 4. Validate change ─────────────────────────────────────
        if modified == original:
            raise InjectionError(
                recipe.id,
                "No changes detected after injection — "
                "the AI instruction or diff patch produced content "
                "identical to the original file",
            )

        logger.info(
            "Injection produced changes",
            recipe_id=recipe.id,
            original_chars=len(original),
            modified_chars=len(modified),
            chars_changed=abs(len(modified) - len(original)),
        )

        # ── 5. Write modified file ─────────────────────────────────
        target.write_text(modified, encoding="utf-8")
        logger.info("Modified file written", path=str(target))
        modified_files = [str(target)]

        # ── 5b. Process extra_files (cross-layer bugs) ─────────────
        for extra in recipe.injection.extra_files:
            extra_path = self.repo_path / extra["file"]
            if not extra_path.is_file():
                logger.warning(
                    "Extra target file does not exist — skipping",
                    file=extra["file"],
                )
                continue
            extra_original = extra_path.read_text(encoding="utf-8")
            extra_instruction = extra.get("instruction", recipe.injection.ai_instruction)
            extra_lang = detect_language(extra_path)
            extra_modified = await self._apply_ai_rewrite(
                recipe.id,
                extra_original,
                extra_instruction,
                extra_lang,
            )
            if extra_modified == extra_original:
                logger.warning(
                    "Extra file produced no changes — skipping",
                    file=extra["file"],
                )
                continue
            extra_path.write_text(extra_modified, encoding="utf-8")
            logger.info("Extra file written", path=str(extra_path))
            modified_files.append(str(extra_path))

        # ── 6. Commit changes (all injected files) ──────────────────
        all_paths = [recipe.injection.target_file] + [
            e["file"] for e in recipe.injection.extra_files
        ]
        commit_hexsha = self.git.commit_changes(
            f"feat(bug-factory): inject bug {recipe.id} - {recipe.title}",
            paths=all_paths,
        )
        logger.info("Changes committed", hexsha=commit_hexsha)

        # ── 7. Compute diff ────────────────────────────────────────
        diff = self.git.diff_against_main()
        logger.info(
            "Injection complete",
            recipe_id=recipe.id,
            branch=branch,
            diff_lines=len(diff.splitlines()) if diff else 0,
        )

        return InjectionResult(
            recipe_id=recipe.id,
            branch=branch,
            diff=diff,
            modified_files=modified_files,
        )

    # ── private helpers ──────────────────────────────────────────────

    async def _apply_ai_rewrite(
        self,
        recipe_id: str,
        original: str,
        instruction: str,
        language: str,
    ) -> str:
        """Delegate code modification to the AI rewriter.

        Args:
            recipe_id: The recipe identifier (for error messages).
            original: The original file content.
            instruction: Natural-language rewrite instruction.
            language: Programming language tag (e.g. ``"python"``).

        Returns:
            The AI-modified source code.

        Raises:
            InjectionError: If the rewriter returns empty or identical content.
        """
        logger.info(
            "Using AI rewriter",
            recipe_id=recipe_id,
            language=language,
        )
        try:
            modified = await self.rewriter.rewrite_file(
                file_content=original,
                instruction=instruction,
                file_language=language,
            )
            return modified
        except Exception as exc:
            raise InjectionError(
                recipe_id,
                f"AI rewriter failed: {exc}",
            ) from exc

    @staticmethod
    def _apply_diff_patch(recipe_id: str, original: str, diff_patch: str) -> str:
        """Apply a unified-diff patch directly.

        Args:
            recipe_id: The recipe identifier (for error messages).
            original: The original file content.
            diff_patch: A unified diff string.

        Returns:
            The patched source code.

        Raises:
            InjectionError: If the patch application fails.
        """
        logger.info("Using diff patch", recipe_id=recipe_id)
        try:
            return DiffPatchApplier.apply(original, diff_patch)
        except Exception as exc:
            raise InjectionError(
                recipe_id,
                f"Diff patch application failed: {exc}",
            ) from exc
