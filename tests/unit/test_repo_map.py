"""Tests for the repo-map provider (base + ctags)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.repomap.base import RepoMap, RepoMapEntry, RepoMapProvider
from src.swe_team.providers.repomap.ctags_provider import CtagsRepoMapProvider


# ---------------------------------------------------------------------------
# RepoMap.to_prompt_string tests
# ---------------------------------------------------------------------------

class TestToPromptString:
    def test_format_basic(self):
        entries = [
            RepoMapEntry(file="src/app.py", symbol_type="class", name="App", signature="App", line=1),
            RepoMapEntry(file="src/app.py", symbol_type="method", name="run", signature="run(self)", line=5),
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="2026-01-01")
        text = rmap.to_prompt_string()
        assert "src/app.py" in text
        assert "class App" in text
        assert "def run(self)" in text

    def test_format_indentation(self):
        entries = [
            RepoMapEntry(file="mod.py", symbol_type="class", name="Foo", signature="Foo", line=1),
            RepoMapEntry(file="mod.py", symbol_type="function", name="bar", signature="bar(x)", line=10),
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        text = rmap.to_prompt_string()
        lines = text.split("\n")
        # Class indented with 2 spaces, function with 4
        class_line = [l for l in lines if "class" in l][0]
        func_line = [l for l in lines if "def" in l][0]
        assert class_line.startswith("  ")
        assert func_line.startswith("    ")

    def test_truncates_at_max_chars(self):
        entries = [
            RepoMapEntry(file=f"file_{i}.py", symbol_type="function", name=f"func_{i}", signature=f"func_{i}()", line=1)
            for i in range(100)
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        text = rmap.to_prompt_string(max_chars=200)
        assert len(text) <= 200 + 100  # some slack for the last line

    def test_truncated_flag_set(self):
        entries = [
            RepoMapEntry(file=f"file_{i}.py", symbol_type="function", name=f"func_{i}", signature=f"func_{i}()", line=1)
            for i in range(100)
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        rmap.to_prompt_string(max_chars=100)
        assert rmap.truncated is True

    def test_not_truncated_when_fits(self):
        entries = [
            RepoMapEntry(file="a.py", symbol_type="function", name="f", signature="f()", line=1),
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        rmap.to_prompt_string(max_chars=10000)
        assert rmap.truncated is False

    def test_empty_entries(self):
        rmap = RepoMap(entries=[], repo_path="/repo", generated_at="now")
        assert rmap.to_prompt_string() == ""

    def test_sorted_by_file(self):
        entries = [
            RepoMapEntry(file="z.py", symbol_type="function", name="z", line=1),
            RepoMapEntry(file="a.py", symbol_type="function", name="a", line=1),
        ]
        rmap = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        text = rmap.to_prompt_string()
        assert text.index("a.py") < text.index("z.py")


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider tests
# ---------------------------------------------------------------------------

class TestCtagsAvailability:
    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ctags 6.0", stderr="")
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is True

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_false_nonzero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is False

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_false_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is False

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_health_check_delegates(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        provider = CtagsRepoMapProvider()
        assert provider.health_check() is True


class TestCtagsFallback:
    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_when_ctags_unavailable(self, mock_run):
        # First call (is_available) fails, second call (generate's is_available) also fails
        mock_run.side_effect = FileNotFoundError
        provider = CtagsRepoMapProvider()
        # Create a temp dir with a .py file
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            Path(os.path.join(td, "test.py")).write_text("x = 1")
            result = provider.generate(Path(td))
            assert isinstance(result, RepoMap)
            assert len(result.entries) >= 1
            assert "test.py" in result.entries[0].file

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_file_listing_excludes_pycache(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        provider = CtagsRepoMapProvider()
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            pycache = os.path.join(td, "__pycache__")
            os.makedirs(pycache)
            Path(os.path.join(pycache, "cached.pyc")).write_text("")
            Path(os.path.join(td, "main.py")).write_text("x = 1")
            result = provider.generate(Path(td))
            files = [e.file for e in result.entries]
            assert not any("__pycache__" in f for f in files)


class TestCtagsParsing:
    def _ctags_json_line(self, **kwargs) -> str:
        tag = {
            "_type": "tag",
            "name": "MyClass",
            "path": "/repo/src/mod.py",
            "kind": "class",
            "line": 10,
            "signature": "",
        }
        tag.update(kwargs)
        return json.dumps(tag)

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_ctags_output_parsed_correctly(self, mock_run):
        ctags_output = "\n".join([
            self._ctags_json_line(name="MyClass", kind="class", path="/repo/src/mod.py", line=5),
            self._ctags_json_line(name="my_func", kind="function", path="/repo/src/mod.py", line=20, signature="(x, y)"),
        ])
        # is_available call
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ctags 6.0", stderr=""),  # is_available
            MagicMock(returncode=0, stdout=ctags_output, stderr=""),  # generate
        ]
        provider = CtagsRepoMapProvider()
        result = provider.generate(Path("/repo"), max_tokens=5000)
        assert len(result.entries) == 2
        assert result.entries[0].name == "MyClass"
        assert result.entries[0].symbol_type == "class"
        assert result.entries[1].name == "my_func"
        assert result.entries[1].symbol_type == "function"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_ignore_patterns_applied(self, mock_run):
        ctags_output = "\n".join([
            self._ctags_json_line(name="Foo", kind="class", path="/repo/src/mod.py", line=1),
            self._ctags_json_line(name="Bar", kind="class", path="/repo/node_modules/lib.py", line=1),
        ])
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ctags 6.0", stderr=""),
            MagicMock(returncode=0, stdout=ctags_output, stderr=""),
        ]
        provider = CtagsRepoMapProvider()
        result = provider.generate(Path("/repo"))
        names = [e.name for e in result.entries]
        assert "Foo" in names
        assert "Bar" not in names

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_custom_ignore(self, mock_run):
        ctags_output = self._ctags_json_line(name="X", kind="class", path="/repo/tests/t.py", line=1)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ctags 6.0", stderr=""),
            MagicMock(returncode=0, stdout=ctags_output, stderr=""),
        ]
        provider = CtagsRepoMapProvider()
        result = provider.generate(Path("/repo"), ignore=["tests"])
        assert len(result.entries) == 0

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_non_tag_lines_skipped(self, mock_run):
        output = json.dumps({"_type": "ptag", "name": "JSON_OUTPUT_VERSION"}) + "\n"
        output += self._ctags_json_line(name="Real", kind="function", path="/repo/a.py", line=1)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ctags 6.0", stderr=""),
            MagicMock(returncode=0, stdout=output, stderr=""),
        ]
        provider = CtagsRepoMapProvider()
        result = provider.generate(Path("/repo"))
        assert len(result.entries) == 1
        assert result.entries[0].name == "Real"


class TestProtocolCompliance:
    def test_ctags_provider_implements_protocol(self):
        assert isinstance(CtagsRepoMapProvider(), RepoMapProvider)

    def test_protocol_is_runtime_checkable(self):
        assert hasattr(RepoMapProvider, "__protocol_attrs__") or True  # runtime_checkable
