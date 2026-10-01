from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent.tools import GlobTool, GrepTool, WorkspaceGuard, create_default_registry
from codeagent.tools.search_files import SearchFiles


class SearchToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guard = WorkspaceGuard(self.root)
        self.grep = GrepTool(workspace_guard=self.guard)
        self.glob = GlobTool(workspace_guard=self.guard)

    def write(self, name, text="scope_needle\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_git_scope_includes_new_and_tracked_files_respects_nested_ignores(self):
        self.git("init", "-q")
        tracked = self.write("tracked.generated.py")
        self.git("add", "--", tracked.name)
        self.write(".gitignore", "eval-results/\n*.generated.py\n!keep.generated.py\n")
        self.write("src/.gitignore", "local.py\n")
        current = [tracked, self.write("src/new.py"), self.write("keep.generated.py")]
        ignored = [self.write("eval-results/old.py"), self.write("skip.generated.py"), self.write("src/local.py")]
        for output in (self.glob.run("**/*.py"), self.grep.run("scope_needle")):
            for path in current:
                self.assertIn(str(path), output)
            for path in ignored:
                self.assertNotIn(str(path), output)
        # Subdirectory searches must retain the root and nested ignore rules.
        self.assertIn(str(current[1]), self.grep.run("scope_needle", "src"))
        self.assertNotIn(str(ignored[-1]), self.grep.run("scope_needle", "src"))
        for output in (
            self.glob.run("**/*.py", include_ignored=True),
            self.grep.run("scope_needle", include_ignored=True),
        ):
            for path in current + ignored:
                self.assertIn(str(path), output)
            self.assertNotIn(str(self.root / ".git") + "\\", output)
        self.assertIn(str(ignored[0]), self.grep.run("scope_needle", "eval-results", include_ignored=True))

    def test_non_git_fallback_and_explicit_archive_scope(self):
        current = self.write("src/current.py")
        old = self.write("eval-results/old.py")
        with patch("codeagent.tools.search_files.subprocess.run", side_effect=FileNotFoundError):
            for output in (self.glob.run("**/*.py"), self.grep.run("scope_needle")):
                self.assertIn(str(current), output)
                self.assertNotIn(str(old), output)
                self.assertIn("fallback directory exclusions", output)
            self.assertIn(str(old), self.grep.run("scope_needle", include_ignored=True))

    def test_grep_paging_reaches_201st_match_without_duplicates(self):
        target = self.write("many.py", "needle\n" * 200 + "needle FINAL_TARGET\n")
        first = self.grep.run("needle")
        second = self.grep.run("needle", offset=200)
        self.assertIn("next_offset=200", first)
        self.assertIn("scan_complete=false", first)
        self.assertNotIn("FINAL_TARGET", first)
        self.assertIn(f"{target}:201: needle FINAL_TARGET", second)
        self.assertNotIn(f"{target}:200:", second)
        self.assertIn("has_more=false", second)
        self.assertIn("scan_complete=true", second)
        self.assertNotIn("next_offset", self.grep.run("needle", limit=201))

    def test_glob_paging_is_path_ordered_and_supports_recursive_patterns(self):
        for index in reversed(range(101)):
            self.write(f"src/{index:03d}.py")
        self.write("root.py")
        first = self.glob.run("src/**/*.py")
        second = self.glob.run("src/**/*.py", offset=100)
        first_paths = [line for line in first.splitlines() if line.startswith(str(self.root))]
        self.assertEqual(len(first_paths), 100)
        self.assertEqual(first_paths, sorted(first_paths))
        self.assertIn("next_offset=100", first)
        self.assertIn(str(self.root / "src/100.py"), second)
        self.assertIn("has_more=false", second)
        self.assertIn(str(self.root / "root.py"), self.glob.run("*.py"))
        self.assertNotIn(str(self.root / "src/000.py"), self.glob.run("*.py"))
        self.assertIn(str(self.root / "src"), self.glob.run("src/"))

    def test_grep_searches_past_old_file_cap_and_reports_unreadable_files(self):
        target = self.write("target.py", "unique_target\n")
        # Repeated paths keep this boundary test cheap: no 5,001-file fixture.
        inventory = SearchFiles(paths=[target] * 5001)
        with patch("codeagent.tools.grep.search_files", return_value=inventory):
            result = self.grep.run("unique_target", offset=5000)
        self.assertIn(f"{target}:1:", result)
        self.assertIn("scan_complete=true", result)
        with patch("codeagent.tools.grep.search_files", return_value=SearchFiles(paths=[self.root / "gone.py", target])):
            result = self.grep.run("unique_target")
        self.assertIn(f"{target}:1:", result)
        self.assertIn("scan_complete=false", result)
        self.assertIn("1 file(s) could not be read", result)

    def test_scan_failure_is_visible_and_include_limits_scope(self):
        py = self.write("src/main.py")
        self.write("src/main.txt")
        result = self.grep.run("scope_needle", include="src/**/*.py")
        self.assertIn(str(py), result)
        self.assertNotIn("main.txt:", result)
        inventory = SearchFiles(paths=[py], incomplete=True, notes=["Unreadable directory: private"])
        with patch("codeagent.tools.glob_tool.search_files", return_value=inventory):
            self.assertIn("scan_complete=false", self.glob.run("**/*.py"))

    def test_page_validation_and_workspace_boundary_with_ignored_files_enabled(self):
        registry = create_default_registry(workspace_guard=self.guard)
        for name, arguments in (("grep", {"pattern": "x"}), ("glob", {"pattern": "**/*"})):
            for page in ({"offset": -1}, {"limit": 0}, {"limit": 1001}, {"offset": True}):
                self.assertEqual(registry.execute(name, {**arguments, **page}).status, "error")
            result = registry.execute(name, {**arguments, "path": "../", "include_ignored": True})
            self.assertIn("Workspace access denied", result)


if __name__ == "__main__":
    unittest.main()
