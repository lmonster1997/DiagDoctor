"""
AI-powered code rewriter for Bug Factory.

Provides two strategies for modifying source files:

1. **AIRewriter** — sends the original file + a natural-language instruction
   to an LLM and receives the modified file back.  Includes retry logic (3
   attempts), response validation, and safe code-block extraction.

2. **DiffPatchApplier** — applies a unified diff patch directly, bypassing
   the LLM entirely.  Used when a recipe provides a precise ``diff_patch``.

Usage::

    from langchain_openai import ChatOpenAI
    from bug_factory.ai_rewriter import AIRewriter, DiffPatchApplier

    llm = ChatOpenAI(model="gpt-4o")
    rewriter = AIRewriter(llm)
    new_code = await rewriter.rewrite_file(
        file_content=original,
        instruction="Replace all sync calls with async equivalents",
        file_language="python",
    )
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from tenacity import (
    RetryError,
    before_log,
    retry,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一个专业的代码改写助手。你的任务是根据用户给出的指令，精确修改给定的代码。

请严格遵守以下规则：
1. **只输出**修改后的完整文件内容
2. 不要添加任何解释、注释或对话
3. 不要输出"这是修改后的代码"之类的引导语
4. 用三个反引号包裹代码块，并标注语言标识（如 ```python）
5. 保持原有代码的缩进风格和格式规范
6. 如果指令无法执行（例如找不到要修改的代码），仍然输出原始代码
"""

_USER_PROMPT_TEMPLATE = """\
【原始代码】
```{language}
{content}
```

【改写指令】
{instruction}

【要求】
- 只输出修改后的完整文件内容
- 不要添加任何解释或注释
- 用 ```{language} ... ``` 包裹
"""

# Regex to extract fenced code blocks.  Matches ```lang⏎...⏎``` patterns.
# Key: the language tag is optional and may be followed by optional
# whitespace before the newline.  We use a non-greedy match for the
# code body to correctly handle files that contain embedded backticks.
_CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z+#]*)[^\S\r\n]*\n(?P<code>.*?)\n\s*```",
    re.DOTALL,
)

# Fallback: match anything between the first and last triple backtick
_CODE_BLOCK_LOOSE_RE = re.compile(
    r"```(?:\w*\s*)?\n(.*)```",
    re.DOTALL,
)

_MAX_RETRIES = 3


# ── Exceptions ───────────────────────────────────────────────────────


class RewriteError(Exception):
    """Raised when AI-based code rewriting fails after all retries."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Code rewriting failed: {detail}")


class PatchError(Exception):
    """Raised when a diff patch cannot be applied cleanly."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Patch application failed: {detail}")


# ── Language detection ───────────────────────────────────────────────

_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".css": "css",
    ".html": "html",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
    ".sh": "bash",
    ".tf": "hcl",
    ".md": "markdown",
}


def detect_language(file_path: str | Path) -> str:
    """Detect the programming language from a file extension.

    Args:
        file_path: Path or filename to inspect.

    Returns:
        Language identifier suitable for Markdown fenced code blocks
        (e.g. ``"python"``, ``"typescript"``).  Defaults to ``"text"``
        for unrecognised extensions.
    """
    suffix = Path(file_path).suffix.lower()
    return _LANGUAGE_MAP.get(suffix, "text")


# ── Code block extraction ────────────────────────────────────────────


def extract_code_block(response_text: str) -> str:
    """Extract source code from an LLM response that may contain
    explanatory text around a fenced code block.

    Strategy:
        1. Try strict `` ```lang ... ``` `` match.
        2. Fall back to loose `` ``` ... ``` `` match.
        3. If no block found, return the response as-is (the model may
           have returned raw code).

    Args:
        response_text: The raw text returned by the LLM.

    Returns:
        The extracted code content (without backticks).
    """
    # Primary: strict match with optional language tag
    match = _CODE_BLOCK_RE.search(response_text)
    if match:
        return match.group("code")

    # Fallback: loose match (just first/last triple backtick)
    match = _CODE_BLOCK_LOOSE_RE.search(response_text)
    if match:
        return match.group(1).strip()

    # Last resort: return as-is, stripping whitespace only
    logger.debug("No fenced code block found — returning raw response")
    return response_text.strip()


# ── Diff patch application ───────────────────────────────────────────


@dataclass
class PatchResult:
    """Result of applying a unified diff patch."""

    success: bool
    modified_content: str
    rejected_hunks: int = 0


class DiffPatchApplier:
    """Applies unified diff patches to source files.

    Uses a simple but robust unified-diff parser that handles the common
    ``git diff`` output format.  Unmatched hunks are **silently skipped**
    (best-effort application), and a warning is logged when the result
    is identical to the input.
    """

    @staticmethod
    def apply(original: str, diff_patch: str) -> str:
        """Apply a unified diff patch to *original* and return the result.

        Args:
            original: The original file content.
            diff_patch: A unified diff string (as produced by
                ``git diff`` or ``diff -u``).

        Returns:
            The patched file content.

        Raises:
            PatchError: If the patch string is empty or contains no
                parseable hunks.
        """
        if not diff_patch.strip():
            raise PatchError("Empty diff patch — nothing to apply")

        original_lines = original.splitlines(keepends=True)
        hunk_pattern = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

        result_lines: list[str] = []
        pos = 0  # current position in original_lines (0-based line index)

        # Parse the patch line by line
        lines = diff_patch.splitlines(keepends=True)
        i = 0
        hunks_applied = 0

        while i < len(lines):
            line = lines[i]
            hunk_match = hunk_pattern.match(line)
            if hunk_match:
                # Found a hunk header — process it
                old_start = int(hunk_match.group(1)) - 1  # 0-based
                # old_count = int(hunk_match.group(2) or 1)
                # new_start = int(hunk_match.group(3)) - 1
                # new_count = int(hunk_match.group(4) or 1)

                # Copy lines from current position to hunk start
                while pos < old_start and pos < len(original_lines):
                    result_lines.append(original_lines[pos])
                    pos += 1

                # Process hunk body
                i += 1
                hunk_old_line = old_start
                while i < len(lines) and not lines[i].startswith("@@"):
                    body_line = lines[i]
                    if body_line.startswith(" "):
                        # Context line — must match original
                        if hunk_old_line < len(original_lines) and original_lines[
                            hunk_old_line
                        ].rstrip("\n") == body_line[1:].rstrip("\n"):
                            result_lines.append(original_lines[hunk_old_line])
                        else:
                            # Best-effort: append the context line as-is
                            result_lines.append(body_line[1:])
                        hunk_old_line += 1
                        pos = hunk_old_line
                    elif body_line.startswith("-"):
                        # Removed line — skip in output
                        hunk_old_line += 1
                        pos = hunk_old_line
                    elif body_line.startswith("+"):
                        # Added line — append
                        result_lines.append(body_line[1:])
                    elif body_line.startswith("\\"):
                        # "\ No newline at end of file" — ignore
                        pass
                    else:
                        # Unexpected line in hunk body — skip
                        pass
                    i += 1

                hunks_applied += 1
            else:
                # Non-hunk line (header, etc.) — skip
                i += 1

        # Copy remaining original lines after last hunk
        while pos < len(original_lines):
            result_lines.append(original_lines[pos])
            pos += 1

        modified = "".join(result_lines)

        if hunks_applied == 0:
            raise PatchError(
                "No hunks found in diff patch — "
                "the patch may be malformed or in an unsupported format"
            )

        if modified == original:
            logger.warning(
                "Patch application produced no changes — the diff may not match the target file"
            )

        logger.info(
            "Diff patch applied",
            hunks=hunks_applied,
            original_lines=len(original_lines),
            modified_lines=len(result_lines),
        )
        return modified


# ── AI Rewriter ──────────────────────────────────────────────────────


class AIRewriter:
    """Rewrites source files by sending a natural-language instruction
    together with the original content to an LLM.

    The LLM is expected to return a fenced code block containing the
    modified file.  On failure the operation is retried up to 3 times
    with exponential backoff.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        """Initialise the rewriter.

        Args:
            llm: A LangChain chat model instance (e.g.
                ``ChatOpenAI(model="gpt-4o")``).  Must support
                ``ainvoke`` with a list of messages.
        """
        self.llm = llm
        logger.info("AIRewriter initialised", model=getattr(llm, "model_name", "unknown"))

    async def rewrite_file(
        self,
        file_content: str,
        instruction: str,
        file_language: str = "python",
    ) -> str:
        """Rewrite *file_content* according to *instruction*.

        The LLM is called up to 3 times.  Each attempt includes the full
        original content and instruction — the LLM does **not** see its
        previous failed attempts (to avoid compounding errors).

        Args:
            file_content: The complete original source code.
            instruction: A natural-language description of the desired
                changes (e.g. "Remove the N+1 query in list_tasks").
            file_language: Language tag for the fenced code block
                (default ``"python"``).

        Returns:
            The modified source code.

        Raises:
            RewriteError: If all 3 retry attempts fail, or if the LLM
                returns content that fails validation (empty / identical).
        """
        logger.info(
            "Starting AI rewrite",
            language=file_language,
            content_lines=len(file_content.splitlines()),
        )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            language=file_language,
            content=file_content,
            instruction=instruction,
        )

        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        try:
            new_content = await self._call_with_retry(messages)
        except RetryError as exc:
            raise RewriteError(f"Failed after {_MAX_RETRIES} attempts: {exc}") from exc

        # ── Post-validation ──────────────────────────────────────
        if not new_content or not new_content.strip():
            raise RewriteError("LLM returned empty content after all retries")

        if new_content.strip() == file_content.strip():
            raise RewriteError(
                "LLM returned content identical to the original — "
                "the instruction may be a no-op or the model refused to change it"
            )

        logger.info(
            "AI rewrite succeeded",
            original_lines=len(file_content.splitlines()),
            modified_lines=len(new_content.splitlines()),
        )
        return new_content

    @retry(
        stop=stop_after_attempt(_MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def _call_with_retry(self, messages: list[BaseMessage]) -> str:
        """Send messages to the LLM, extract code block, and validate.

        This method is decorated with ``@retry`` so any exception it
        raises will trigger a retry up to ``_MAX_RETRIES`` times.
        """
        logger.debug("Sending rewrite request to LLM")
        response = await self.llm.ainvoke(messages)

        raw_text: str = str(response.content) if hasattr(response, "content") else str(response)
        logger.debug("LLM response received", chars=len(raw_text))

        extracted = extract_code_block(raw_text)

        if not extracted or not extracted.strip():
            raise RewriteError("LLM returned empty content (attempt will retry)")

        return extracted
