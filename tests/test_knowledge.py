"""Tests for knowledge indexing module."""

import os
import tempfile
import pytest
from pathlib import Path
from shadow_ai.knowledge import _build_knowledge_index, _build_codebase_index
from shadow_ai.config import CODEBASE_INDEX_EXTENSIONS, CODEBASE_PATTERNS


@pytest.fixture
def knowledge_dir():
    """Create a temp directory with knowledge files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        (Path(tmpdir) / "guide.md").write_text("# User Guide\n\nThis is the guide.\n\n## Getting Started\n\nDo this.")
        (Path(tmpdir) / "notes.txt").write_text("Some notes here\nMore notes")
        (Path(tmpdir) / "big_file.md").write_text("x" * 50_000)  # Bigger than inline threshold
        (Path(tmpdir) / "ignored.bin").write_text("binary stuff")  # Not indexable
        yield tmpdir


@pytest.fixture
def code_dir():
    """Create a temp directory with code files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "app.py").write_text(
            "def hello():\n    pass\n\nclass MyClass:\n    pass\n\nasync def fetch_data():\n    pass\n"
        )
        (Path(tmpdir) / "utils.js").write_text(
            "function doStuff() {}\nconst helper = async () => {}\nexport class Widget {}\n"
        )
        yield tmpdir


class TestBuildKnowledgeIndex:
    def test_indexes_files(self, knowledge_dir):
        index, inline, dirs = _build_knowledge_index([knowledge_dir])
        # Small files may be inlined, big file should be in index
        assert "big_file.md" in index or "guide.md" in inline
        # On macOS, /var -> /private/var symlink may differ
        assert any(knowledge_dir in d or d.endswith(os.path.basename(knowledge_dir)) for d in dirs)

    def test_skips_non_indexable(self, knowledge_dir):
        index, inline, dirs = _build_knowledge_index([knowledge_dir])
        assert "ignored.bin" not in index

    def test_inlines_small_files(self, knowledge_dir):
        index, inline, dirs = _build_knowledge_index(
            [knowledge_dir],
            inline_threshold=100_000,
            total_inline_limit=200_000,
        )
        assert "User Guide" in inline or "notes" in inline

    def test_empty_paths(self):
        index, inline, dirs = _build_knowledge_index([])
        assert index == ""
        assert inline == ""
        assert dirs == []


class TestBuildCodebaseIndex:
    def test_extracts_python_signatures(self, code_dir):
        index = _build_codebase_index(
            [code_dir],
            max_size=50_000,
            extensions=CODEBASE_INDEX_EXTENSIONS,
            patterns=CODEBASE_PATTERNS,
        )
        assert "hello" in index
        assert "MyClass" in index
        assert "fetch_data" in index

    def test_extracts_js_signatures(self, code_dir):
        index = _build_codebase_index(
            [code_dir],
            max_size=50_000,
            extensions=CODEBASE_INDEX_EXTENSIONS,
            patterns=CODEBASE_PATTERNS,
        )
        assert "doStuff" in index or "Widget" in index

    def test_empty_paths(self):
        index = _build_codebase_index(
            [],
            max_size=50_000,
            extensions=CODEBASE_INDEX_EXTENSIONS,
            patterns=CODEBASE_PATTERNS,
        )
        assert index == ""
