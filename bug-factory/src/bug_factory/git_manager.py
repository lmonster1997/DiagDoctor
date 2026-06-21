"""
Git branch management for Bug Factory.

Provides GitManager, a safe wrapper around gitpython for creating bug
branches, committing changes, resetting state, and computing diffs.
All operations are logged via structlog and raise GitOperationError on failure.

Usage:
    from bug_factory.git_manager import GitManager

    gm = GitManager(Path("/path/to/repo"))
    branch = gm.create_bug_branch("BE-001")       # → "bug/BE-001"
    gm.commit_changes("Inject bug: BE-001 - N+1 query")
    diff = gm.diff_against_main()                  # → unified diff string
    gm.reset_to_main()                             # clean up
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import structlog
from git import GitCommandError, InvalidGitRepositoryError, Repo

logger = structlog.get_logger(__name__)


class GitOperationError(Exception):
    """Raised when a Git operation fails irrecoverably."""

    def __init__(self, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"Git operation '{operation}' failed: {detail}")


class GitManager:
    """Encapsulates safe Git operations for bug injection workflow.

    All mutations are performed against a single repository identified
    by ``repo_path``.  The manager always operates from the ``main``
    baseline — bug branches are created from ``main`` and can be reset
    back to it.
    """

    def __init__(self, repo_path: Path) -> None:
        """Initialise the manager and validate the target repository.

        Args:
            repo_path: Absolute or relative path to a git repository
                root (the directory containing ``.git``).

        Raises:
            GitOperationError: If ``repo_path`` is not a valid git
                repository or does not have a ``main`` branch.
        """
        self.repo_path = repo_path.resolve()
        logger.info(
            "Initialising GitManager",
            repo_path=str(self.repo_path),
        )

        try:
            self._repo = Repo(self.repo_path)
        except InvalidGitRepositoryError as exc:
            raise GitOperationError(
                "init",
                f"Not a valid git repository: {self.repo_path}",
            ) from exc

        # Ensure main branch exists
        try:
            self._main_branch = self._repo.heads.main
        except AttributeError as exc:
            raise GitOperationError(
                "init",
                "Repository does not have a 'main' branch. Bug branches must be created from main.",
            ) from exc

        # Cache the initial branch so reset_to_main can return to it
        self._initial_branch: str = self._get_active_branch_name()

    # ── helpers ──────────────────────────────────────────────────────

    def _get_active_branch_name(self) -> str:
        """Return the name of the currently checked-out branch."""
        try:
            return self._repo.active_branch.name
        except TypeError:
            # Detached HEAD state
            return "HEAD"

    def _force_checkout(self, target: str) -> None:
        """Check out *target* branch, discarding local changes."""
        logger.debug("Force-checkout", target=target)
        self._repo.git.checkout(target, force=True)

    def _stash_if_dirty(self) -> bool:
        """Stash uncommitted changes. Returns True if anything was stashed."""
        if self._repo.is_dirty(untracked_files=True):
            logger.warning(
                "Repository is dirty — stashing changes before operation",
                repo_path=str(self.repo_path),
            )
            self._repo.git.stash("push", "--include-untracked")
            return True
        return False

    def _pop_stash(self) -> None:
        """Pop the most recent stash if one exists."""
        with suppress(GitCommandError):
            self._repo.git.stash("pop")

    # ── public API ───────────────────────────────────────────────────

    def get_current_branch(self) -> str:
        """Return the name of the currently active branch.

        Returns:
            Branch name, or ``"HEAD"`` if in detached HEAD state.
        """
        branch = self._get_active_branch_name()
        logger.debug("Current branch", branch=branch)
        return branch

    def create_bug_branch(self, recipe_id: str) -> str:
        """Create (or force-reset) a bug branch from ``main``.

        The branch is named ``bug/{recipe_id}``.  If a branch with that
        name already exists it is **force-deleted** and recreated from
        the current tip of ``main``.

        Steps:
            1. Stash any uncommitted changes.
            2. Checkout ``main`` and pull latest (fast-forward only).
            3. Delete local ``bug/{recipe_id}`` branch if present.
            4. Create new branch ``bug/{recipe_id}`` from ``main``.

        Args:
            recipe_id: The bug recipe identifier (e.g. ``"BE-001"``).

        Returns:
            The full branch name (``"bug/BE-001"``).

        Raises:
            GitOperationError: If any Git step fails (e.g. main cannot
                be checked out, branch deletion fails, etc.).
        """
        branch_name = f"bug/{recipe_id}"
        logger.info("Creating bug branch", recipe_id=recipe_id, branch=branch_name)

        stashed = self._stash_if_dirty()

        try:
            # 1. Ensure we're on a clean main
            self._force_checkout("main")
            try:
                self._repo.remotes.origin.pull("main", ff_only=True)
                logger.debug("Pulled latest main")
            except GitCommandError:
                logger.warning("Could not pull main (no remote or network issue)")

            # 2. Delete existing bug branch if present
            if branch_name in self._repo.heads:
                logger.info(
                    "Deleting existing bug branch",
                    branch=branch_name,
                )
                self._repo.delete_head(branch_name, force=True)

            # 3. Create new branch from main
            new_branch = self._repo.create_head(branch_name)
            new_branch.checkout()
            logger.info("Bug branch created", branch=branch_name)

        except GitCommandError as exc:
            raise GitOperationError(
                "create_bug_branch",
                f"Failed to create branch '{branch_name}': {exc.stderr}",
            ) from exc
        finally:
            if stashed:
                self._pop_stash()

        return branch_name

    def commit_changes(self, message: str, paths: list[str] | None = None) -> str:
        """Stage changes and commit with *message*.

        Args:
            message: Commit message (should follow Conventional Commits).
            paths: If provided, only stage these specific file paths
                (relative to repo root).  If ``None``, stages all changes.

        Returns:
            The commit hexsha of the new commit.

        Raises:
            GitOperationError: If there are no changes to commit, or if
                the commit operation fails.
        """
        logger.info("Committing changes", message=message, paths=paths)

        # Check dirtiness: if paths specified, only check those paths
        if paths:
            dirty = any(self._repo.is_dirty(path=p, untracked_files=True) for p in paths)
            if not dirty:
                raise GitOperationError(
                    "commit_changes",
                    f"No changes to commit in specified paths: {paths}",
                )
        elif not self._repo.is_dirty(untracked_files=True):
            raise GitOperationError(
                "commit_changes",
                "No changes to commit — the working tree is clean.",
            )

        try:
            if paths:
                self._repo.git.add(*paths)
            else:
                self._repo.git.add(A=True)
            commit = self._repo.index.commit(message)
            logger.info("Changes committed", hexsha=commit.hexsha)
        except GitCommandError as exc:
            raise GitOperationError(
                "commit_changes",
                f"Commit failed: {exc.stderr}",
            ) from exc

        return commit.hexsha

    def reset_to_main(self) -> None:
        """Discard all local changes and switch back to ``main``.

        Any uncommitted changes are **lost**.  After this call the
        working tree is clean on the ``main`` branch.  If the repo was
        initially on a different branch, that original branch is NOT
        restored — always goes to main.
        """
        logger.info("Resetting to main", previous_branch=self.get_current_branch())

        try:
            self._force_checkout("main")
            # Discard any stray changes on main
            self._repo.git.clean("-fd")
            self._repo.git.reset("--hard", "HEAD")
            logger.info("Reset to main complete")
        except GitCommandError as exc:
            raise GitOperationError(
                "reset_to_main",
                f"Failed to reset to main: {exc.stderr}",
            ) from exc

    def diff_against_main(self) -> str:
        """Compute the unified diff between the current branch and ``main``.

        Returns:
            A unified diff string (``git diff main...HEAD``) showing
            all changes introduced on the current branch since it
            diverged from ``main``.  Returns an empty string if there
            are no differences.

        Raises:
            GitOperationError: If the diff operation fails.
        """
        current = self.get_current_branch()
        logger.debug("Computing diff against main", branch=current)

        try:
            raw = self._repo.git.diff("main...HEAD", unified=3)
            diff_text: str = str(raw) if raw else ""
        except GitCommandError as exc:
            raise GitOperationError(
                "diff_against_main",
                f"Diff failed: {exc.stderr}",
            ) from exc

        logger.info(
            "Diff computed",
            branch=current,
            diff_lines=len(diff_text.splitlines()) if diff_text else 0,
        )
        return diff_text

    def get_commit_log(self, max_count: int = 10) -> list[dict[str, str]]:
        """Return the last *max_count* commits as a list of dicts.

        Each dict has keys: ``hexsha``, ``message``, ``author``, ``date``.

        Args:
            max_count: Maximum number of commits to return.

        Returns:
            List of commit info dicts, most recent first.
        """
        commits: list[dict[str, str]] = []
        for commit in self._repo.iter_commits(max_count=max_count):
            commits.append(
                {
                    "hexsha": commit.hexsha,
                    "message": str(commit.message).strip(),
                    "author": str(commit.author),
                    "date": commit.committed_datetime.isoformat(),
                }
            )
        logger.debug("Retrieved commit log", count=len(commits))
        return commits
