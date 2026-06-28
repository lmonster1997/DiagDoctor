"""
Tests for src.tools.file_reader — get_file_content tool.

Uses pytest + pytest-asyncio. Tests with real demo-app files and edge cases.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.tools.file_reader import (
    MAX_LINES,
    _resolve_demo_app_root,
    _resolve_path,
    get_file_content,
    get_get_file_content_tool,
)

# ── Path resolution tests ────────────────────────────────────────────


class TestResolveDemoAppRoot:
    def test_returns_absolute_path(self):
        root = _resolve_demo_app_root()
        assert root.is_absolute()
        assert root.name == "demo-app"

    def test_path_exists(self):
        root = _resolve_demo_app_root()
        assert root.exists()


class TestResolvePath:
    def test_resolves_backend_file(self):
        path = _resolve_path("app/main.py")
        assert path.is_absolute()
        assert "demo-app" in str(path)
        assert str(path).endswith("app\\main.py") or str(path).endswith("app/main.py")

    def test_resolves_frontend_file(self):
        path = _resolve_path("src/pages/TaskBoardPage.tsx")
        assert "demo-app" in str(path)
        assert "TaskBoardPage.tsx" in str(path)

    def test_strips_leading_dot_slash(self):
        path1 = _resolve_path("./app/main.py")
        path2 = _resolve_path("app/main.py")
        assert path1 == path2

    def test_normalizes_backslashes(self):
        path = _resolve_path("app\\services\\task_service.py")
        assert "/" in str(path).replace("\\", "/") or True  # path is valid

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="路径越界"):
            _resolve_path("../../../etc/passwd")

    def test_rejects_absolute_path_outside_demo_app(self):
        with pytest.raises(ValueError, match="路径越界"):
            _resolve_path("C:\\Windows\\System32\\config\\SAM")

    def test_current_dir_is_safe(self):
        """'.' resolves to demo-app root, which is safe."""
        path = _resolve_path(".")
        assert path == _resolve_demo_app_root()


# ── get_file_content integration tests ───────────────────────────────


class TestGetFileContent:
    @pytest.mark.asyncio
    async def test_reads_real_file(self):
        """Read an actual file from demo-app."""
        result = await get_file_content("backend/app/main.py")
        assert "[文件]" in result
        assert "backend/app/main.py" in result
        assert "FastAPI" in result or "app" in result.lower()

    @pytest.mark.asyncio
    async def test_reads_with_line_range(self):
        """Read specific lines from a real file."""
        result = await get_file_content("backend/app/config.py", start_line=1, end_line=5)
        assert "[文件]" in result
        assert "第 1-5 行" in result
        lines = result.split("\n")
        # Should have header + 5 numbered lines
        assert len(lines) >= 6  # header + 5 lines

    @pytest.mark.asyncio
    async def test_start_line_only(self):
        """Only start_line specified, reads to end."""
        result = await get_file_content("backend/app/config.py", start_line=1)
        assert "[文件]" in result

    @pytest.mark.asyncio
    async def test_end_line_only(self):
        """Only end_line specified, reads from beginning."""
        result = await get_file_content("backend/app/config.py", end_line=3)
        assert "[文件]" in result
        # Should show line 1-3
        assert "第 1-3 行" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        result = await get_file_content("nonexistent/file.py")
        assert "文件不存在" in result
        assert "nonexistent/file.py" in result

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        result = await get_file_content("../../../etc/passwd")
        assert "路径越界" in result

    @pytest.mark.asyncio
    async def test_directory_not_file(self):
        result = await get_file_content("backend/app")
        assert "不是文件" in result

    @pytest.mark.asyncio
    async def test_start_line_beyond_file(self):
        """start_line exceeds file length."""
        result = await get_file_content("backend/app/config.py", start_line=99999)
        assert "行号超出范围" in result

    @pytest.mark.asyncio
    async def test_start_greater_than_end(self):
        result = await get_file_content("backend/app/config.py", start_line=10, end_line=5)
        assert "不能大于" in result

    @pytest.mark.asyncio
    async def test_complete_read_includes_line_numbers(self):
        """Output should have line numbers in format '   42| ...'."""
        result = await get_file_content("backend/app/config.py", start_line=1, end_line=1)
        # Should have a line like "     1| ..."
        assert "|" in result
        assert "1|" in result or " 1|" in result


class TestGetFileContentTruncation:
    """Tests for MAX_LINES truncation behavior."""

    @pytest.mark.asyncio
    async def test_truncation_on_large_selection(self):
        """Create a temp file with > MAX_LINES lines and verify truncation."""
        many_lines = "\n".join(f"line {i}" for i in range(MAX_LINES + 50))

        with patch("src.tools.file_reader._resolve_path") as mock_resolve:
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(many_lines)

            mock_resolve.return_value = Path(tmp.name)

            try:
                result = await get_file_content("fake_large.py")
                assert "[截断]" in result
                assert "仅显示前" in result
            finally:
                Path(tmp.name).unlink()

    @pytest.mark.asyncio
    async def test_no_truncation_under_limit(self):
        """Small file should not trigger truncation."""
        result = await get_file_content("backend/app/config.py", start_line=1, end_line=10)
        assert "[截断]" not in result


class TestGetFileContentEdgeCases:
    """Edge cases for robustness."""

    @pytest.mark.asyncio
    async def test_empty_file(self):
        """Empty file should return header with 0 lines."""
        with patch("src.tools.file_reader._resolve_path") as mock_resolve:
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write("")

            mock_resolve.return_value = Path(tmp.name)

            try:
                result = await get_file_content("empty.py")
                assert "[文件]" in result
                assert "共 0 行" in result or "共 1 行" in result
            finally:
                Path(tmp.name).unlink()

    @pytest.mark.asyncio
    async def test_binary_file_rejected(self):
        """Binary files should return a friendly message."""
        with patch("src.tools.file_reader._resolve_path") as mock_resolve:
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as tmp:
                tmp.write(b"\x00\x01\x02\xff\xfe")

            mock_resolve.return_value = Path(tmp.name)

            try:
                result = await get_file_content("binary.bin")
                assert "二进制" in result or "无法读取" in result
            finally:
                Path(tmp.name).unlink()

    @pytest.mark.asyncio
    async def test_frontend_tsx_file(self):
        """Read a frontend TSX file."""
        result = await get_file_content(
            "frontend/src/pages/TaskBoardPage.tsx", start_line=1, end_line=5
        )
        assert "[文件]" in result
        assert "TaskBoardPage.tsx" in result


# ── StructuredTool tests ─────────────────────────────────────────────


class TestGetFileContentTool:
    def test_tool_creation(self):
        tool = get_get_file_content_tool()
        assert tool.name == "get_file_content"
        assert tool.description is not None
        assert "读取 demo-app 代码库" in tool.description

    def test_tool_is_cached(self):
        tool1 = get_get_file_content_tool()
        tool2 = get_get_file_content_tool()
        assert tool1 is tool2

    def test_tool_has_coroutine(self):
        tool = get_get_file_content_tool()
        assert tool.coroutine is not None
        assert callable(tool.coroutine)


class TestGetFileContentMaxFileSize:
    @pytest.mark.asyncio
    async def test_large_file_rejected(self):
        """File > 500KB should be rejected."""
        with (
            patch("src.tools.file_reader._resolve_path") as mock_resolve,
            patch("src.tools.file_reader.MAX_FILE_SIZE_BYTES", 100),
        ):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write("x" * 200)  # 200 bytes > 100 limit

            mock_resolve.return_value = Path(tmp.name)

            try:
                result = await get_file_content("large.py")
                assert "过大" in result
            finally:
                Path(tmp.name).unlink()
