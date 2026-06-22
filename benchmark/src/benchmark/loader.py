"""Case Loader — loads and filters evaluation cases from YAML files.

Provides :class:`CaseLoader` for reading :class:`EvaluationCase` instances
from the benchmark cases directory, optionally filtering by metadata fields
or by tags (case-id prefixes).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml

from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)

# Default directory where evaluation case YAML files live.
_DEFAULT_CASES_DIR = Path(__file__).resolve().parent.parent.parent / "cases"


def _parse_case_yaml(path: Path) -> EvaluationCase:
    """Parse a single evaluation-case YAML file.

    Args:
        path: Absolute or relative path to a ``.yaml`` case file.

    Returns:
        A validated :class:`EvaluationCase`.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the YAML is malformed or fails Pydantic validation.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Case file not found: {resolved}")

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {resolved}, got {type(raw).__name__}")

    return EvaluationCase.model_validate(raw)


class CaseLoader:
    """Load and filter evaluation cases for benchmark runs.

    Typical usage::

        loader = CaseLoader()
        case = loader.load_one("BE-001")
        suite = loader.load_suite(filter={"category": "performance"})
        filtered = loader.filter_by_tags(suite, ["BE", "FE"])

    Args:
        cases_dir: Directory containing ``.yaml`` case files.
            Defaults to ``benchmark/cases/``.
    """

    def __init__(self, cases_dir: str | Path | None = None) -> None:
        if cases_dir is None:
            cases_dir = _DEFAULT_CASES_DIR
        self.cases_dir = Path(cases_dir).resolve()
        if not self.cases_dir.is_dir():
            raise NotADirectoryError(f"Cases directory not found: {self.cases_dir}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_one(self, case_id: str) -> EvaluationCase:
        """Load a single evaluation case by its **case_id**.

        The file is expected at ``{cases_dir}/{case_id}.yaml``.

        Args:
            case_id: The case identifier (e.g. ``"BE-001"``).

        Returns:
            The validated :class:`EvaluationCase`.

        Raises:
            FileNotFoundError: If the corresponding YAML file does not exist.
        """
        path = self.cases_dir / f"{case_id}.yaml"
        logger.debug("Loading case", case_id=case_id, path=str(path))
        return _parse_case_yaml(path)

    def load_suite(
        self,
        filter: dict[str, Any] | None = None,
    ) -> list[EvaluationCase]:
        """Load all evaluation cases under *cases_dir*, optionally filtered.

        Args:
            filter: Optional dictionary of ``field: value`` pairs to match
                against the deserialized case dict **before** Pydantic
                validation.  Supports nested lookups via ``__``, e.g.
                ``{"expected__category": "performance"}``.

        Returns:
            A (possibly empty) list of :class:`EvaluationCase` instances,
            sorted by ``case_id``.

        Examples:
            >>> loader = CaseLoader()
            >>> # All cases
            >>> loader.load_suite()
            >>> # Only performance bugs
            >>> loader.load_suite({"expected__category": "performance"})
        """
        cases: list[EvaluationCase] = []
        for yaml_file in sorted(self.cases_dir.glob("*.yaml")):
            if not yaml_file.is_file():
                continue
            try:
                case = _parse_case_yaml(yaml_file)
            except Exception:
                logger.warning("Failed to parse case file", path=str(yaml_file), exc_info=True)
                continue

            if filter is not None and not self._matches(case, filter):
                continue

            cases.append(case)

        logger.info("Loaded cases", count=len(cases), filter=filter)
        return cases

    @staticmethod
    def filter_by_tags(
        cases: list[EvaluationCase],
        tags: list[str],
    ) -> list[EvaluationCase]:
        """Filter a list of cases by **tags**.

        A case matches if **any** of its tag-like attributes intersect with
        the requested tags.  Currently matches against:

        * The case-id prefix (e.g. ``"BE"`` matches ``"BE-001"``)
        * The ``expected.category`` field

        Args:
            cases: List of cases to filter.
            tags: Tag strings to match (case-insensitive).

        Returns:
            A new list of cases that match at least one tag.
        """
        if not tags:
            return list(cases)

        tag_set = {t.lower() for t in tags}
        result: list[EvaluationCase] = []

        for case in cases:
            own_tags: set[str] = set()

            # Derive tags from case_id prefix (e.g. "BE-001" → "be")
            prefix = case.case_id.split("-")[0] if "-" in case.case_id else case.case_id
            own_tags.add(prefix.lower())

            # Derive tags from expected category
            own_tags.add(case.expected.category.lower())

            # Derive tags from recipe_id prefix
            recipe_prefix = (
                case.recipe_id.split("-")[0] if "-" in case.recipe_id else case.recipe_id
            )
            own_tags.add(recipe_prefix.lower())

            if own_tags & tag_set:
                result.append(case)

        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(case: EvaluationCase, filter: dict[str, Any]) -> bool:
        """Check whether *case* satisfies all filter constraints.

        Supports nested lookups via ``__`` separator, e.g.
        ``{"expected__category": "logic"}`` matches
        ``case.expected.category == "logic"``.

        Flat keys (no ``__``) are applied against the top-level
        :class:`EvaluationCase` fields (``case_id``, ``recipe_id``,
        ``generated_at``).
        """
        for key, expected in filter.items():
            # Allow regex matching when the filter value is a compiled pattern
            if isinstance(expected, re.Pattern):
                actual = CaseLoader._get_nested_attr(case, key)
                actual_str = str(actual) if actual is not None else ""
                if not expected.search(actual_str):
                    return False
            else:
                actual = CaseLoader._get_nested_attr(case, key)
                if actual != expected:
                    return False
        return True

    @staticmethod
    def _get_nested_attr(obj: Any, key: str) -> Any:
        """Resolve a ``__``-separated key path on an object.

        >>> _get_nested_attr(case, "expected__category")
        "performance"
        >>> _get_nested_attr(case, "case_id")
        "BE-001"
        """
        parts = key.split("__")
        for part in parts:
            if hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict):
                obj = obj.get(part)
            else:
                # Attempt to access via dict-like methods (e.g. Pydantic model_dump)
                try:
                    d = obj.model_dump()
                    obj = d.get(part)
                except (AttributeError, KeyError):
                    return None
        return obj
