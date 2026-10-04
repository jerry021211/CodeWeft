from __future__ import annotations

from io import StringIO
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeagent.context import ContextConfig, ContextManager
from codeagent.context.projection import build_tool_projection
from codeagent.messages import ToolUse
from codeagent.tools import LoadToolOutputTool


class ToolOutputPagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output_root = self.root / "outputs"
        self.output_root.mkdir()
        self.tool = LoadToolOutputTool(self.output_root)

    def write(self, text, name="result.txt"):
        path = self.output_root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_existing_line_ranges_and_cross_line_character_pages(self):
        path = self.write("one\ntwo\nthree\n")
        middle = self.tool.run(str(path), offset=2, limit=1)
        self.assertIn("2\ttwo", middle)
        self.assertEqual(middle.next_cursor, {"offset": 3, "char_offset": 0})
        page = self.tool.run(str(path), offset=2, char_offset=1, char_limit=10)
        self.assertIn("2\two", page)
        self.assertNotIn("3\t", page.body)  # The next ordinary line is kept whole.
        self.assertEqual(page.next_cursor, {"offset": 3, "char_offset": 0})
        tail = self.tool.run(str(path), offset=3, char_offset=2)
        self.assertIn("3\tree", tail)
        self.assertNotIn("more_output=true", tail)

    def test_actual_archived_single_line_middle_is_accessible_without_rearchiving(self):
        manager = ContextManager(config=ContextConfig(tool_output_dir=self.output_root))
        raw = "开头😀" * 30_000 + "CRITICAL_MIDDLE" + "结尾" * 50_000
        initial = manager.finalize_tool_results([ToolUse("large", "grep", {"pattern": "x"})], [raw])[0]
        path = Path(manager.state.tool_artifacts[0])
        self.assertIn(initial.output_id, initial)
        before = path.read_bytes()
        page = self.tool.run(str(path), offset=1, limit=1, char_offset=90_000, char_limit=100)
        self.assertIn("CRITICAL_MIDDLE", page)
        self.assertEqual(page.next_cursor, {"offset": 1, "char_offset": 90098})
        self.assertLessEqual(len(page), 122_000)
        finalized = manager.finalize_tool_results(
            [ToolUse("reread", "load_tool_output", {"file_path": str(path), "char_offset": 90_000})], [page],
        )[0]
        self.assertEqual(finalized, page)
        self.assertEqual(len(manager.state.tool_artifacts), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_exact_unicode_long_line_can_be_reconstructed_by_returned_cursors(self):
        raw = "甲😀乙" * 30 + "TAIL"
        path = self.write(raw)
        pieces = []
        offset = 0
        for _ in range(20):
            page = self.tool.run(str(path), char_offset=offset, char_limit=13)
            body = page.body[2:]
            pieces.append(body)
            cursor = page.next_cursor
            if cursor is None:
                break
            next_offset = cursor["char_offset"]
            self.assertGreater(next_offset, offset)
            offset = next_offset
        self.assertEqual("".join(pieces), raw)

    def test_total_response_including_thousands_of_line_headers_is_bounded(self):
        path = self.write("\n" * 6000)
        page = self.tool.run(str(path), limit=5000)
        self.assertLessEqual(len(page), 122_000)
        cursor = page.next_cursor
        self.assertEqual(cursor, {'offset': 5001, 'char_offset': 0})
        following = self.tool.run(str(path), offset=cursor['offset'], limit=1)
        self.assertIn('5001\t', following.body)

    def test_reader_never_loads_an_entire_file_or_unbounded_line(self):
        path = self.write("placeholder")

        class BoundedReader(StringIO):
            def readline(self, size=-1):
                if not 0 < size <= 120001:
                    raise AssertionError("unbounded line allocation")
                return super().readline(size)

        with patch.object(Path, "read_text", side_effect=AssertionError("read_text is unbounded")), patch.object(
            Path, "open", return_value=BoundedReader("prefix\n" + "x" * 200_000 + "TAIL"),
        ):
            page = self.tool.run(str(path), offset=2, char_offset=200_000, char_limit=6)
        self.assertIn("2\tTAIL", page)
        self.assertNotIn("more_output=true", page)

    def test_projection_retains_real_archive_path_and_can_read_exact_middle(self):
        manager = ContextManager(config=ContextConfig(tool_output_dir=self.output_root))
        raw = "x" * 90_000 + "HISTORICAL_PROOF" + "y" * 90_000
        received = manager.finalize_tool_results([ToolUse("cmd", "bash", {"command": "once"})], [raw])[0]
        path = manager.state.tool_artifacts[0]
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "cmd", "name": "bash", "input": {"command": "once"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "cmd", "content": received}]},
        ]
        # Command previews are now smaller than the default cleanup threshold.
        # Force cleanup here to verify that the archive handle survives it.
        projected = build_tool_projection(messages, command_keep=0, min_chars=1000)
        note = projected[1]["content"][0]["content"]
        import json
        self.assertIn(received.output_id, note)
        self.assertIn("load_tool_output", note)
        self.assertIn("HISTORICAL_PROOF", self.tool.run(path, char_offset=90_000, char_limit=100))
        self.assertEqual(messages[1]["content"][0]["content"], received)
        self.assertEqual(projected, build_tool_projection(projected, command_keep=0, min_chars=1000))

    def test_outside_traversal_missing_and_invalid_ranges_are_denied(self):
        other = self.root / "private.txt"
        other.write_text("private-secret", encoding="utf-8")
        for path in (str(other), "../private.txt", "missing.txt", ""):
            with self.subTest(path=path):
                output = self.tool.run(path)
                self.assertTrue(output.startswith("Error:"))
                self.assertNotIn("private-secret", output)
        for key, value in (("offset", 0), ("offset", "1"), ("limit", 5001), ("char_offset", -1),
                           ("char_limit", 0), ("char_limit", 120001), ("char_limit", True)):
            with self.subTest(key=key, value=value):
                self.assertTrue(self.tool.run("missing.txt", **{key: value}).startswith("Error:"))

    def test_file_root_and_parent_links_or_junctions_are_denied(self):
        path = self.write("private-secret")
        for linked in (path, self.output_root, self.root):
            with self.subTest(linked=linked), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda item: item == linked):
                output = self.tool.run(str(path))
                self.assertIn("symbolic links", output)
                self.assertNotIn("private-secret", output)
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400)), patch.object(Path, "is_symlink", return_value=False):
            self.assertIn("junctions", self.tool.run(str(path)))

    def test_eof_and_offsets_beyond_a_line_are_finite_and_explicit(self):
        path = self.write("one\ntwo\n")
        self.assertFalse(self.tool.run(str(path), offset=3).has_more)
        self.assertEqual(self.tool.run(str(path), offset=3).body, '')
        page = self.tool.run(str(path), char_offset=100, limit=1)
        self.assertTrue(page.startswith('Error:'))



if __name__ == "__main__":
    unittest.main()
