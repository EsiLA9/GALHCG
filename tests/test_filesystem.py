from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_preview.filesystem import (
    MAX_BROWSE_OUTPUT_BYTES,
    MAX_PREVIEW_OUTPUT_BYTES,
    MAX_PREVIEW_READ_BYTES,
    ProjectFiles,
    ProjectSetupError,
)


class ProjectFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_files(self, files: dict[str, bytes | str]) -> None:
        for name, contents in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(contents, str):
                path.write_text(contents, encoding="utf-8", newline="")
            else:
                path.write_bytes(contents)

    def service(self, *excludes: str) -> ProjectFiles:
        return ProjectFiles(self.root, extra_excludes=list(excludes))

    def test_root_must_be_absolute_and_readable_directory(self) -> None:
        with self.assertRaises(ProjectSetupError):
            ProjectFiles(Path("relative"))
        with self.assertRaises(ProjectSetupError):
            ProjectFiles(self.root / "missing")
        file = self.root / "file.txt"
        file.write_text("text", encoding="utf-8")
        with self.assertRaises(ProjectSetupError):
            ProjectFiles(file)

    def test_browse_sorted_paged_and_hard_limited(self) -> None:
        self.make_files({"z.txt": "z", "a.txt": "a", "目录/文件.py": "值"})
        service = self.service()
        first = service.browse(limit=1)
        self.assertTrue(first["ok"])
        self.assertEqual(first["entries"], [{"path": "a.txt", "type": "file"}])
        self.assertEqual(first["pagination"]["next_offset"], 1)
        second = service.browse(limit=1, offset=1)
        self.assertEqual(second["entries"], [{"path": "z.txt", "type": "file"}])
        clamped = service.browse(limit=100_000)
        self.assertEqual(clamped["pagination"]["limit"], 500)
        self.assertLessEqual(service._json_size(clamped), MAX_BROWSE_OUTPUT_BYTES)

    def test_browse_total_output_budget_is_enforced(self) -> None:
        for index in range(300):
            name = f"{index:03d}-" + "x" * 200 + ".txt"
            (self.root / name).write_text("x", encoding="utf-8")
        result = self.service().browse(limit=500)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pagination"]["output_limited"])
        self.assertIsNotNone(result["pagination"]["next_offset"])
        self.assertLessEqual(self.service()._json_size(result), MAX_BROWSE_OUTPUT_BYTES)

    def test_nested_gitignore_negation_and_default_excludes(self) -> None:
        self.make_files(
            {
                ".gitignore": "root-secret.txt\n*.log\n!keep.log\nlocked/\n!locked/keep.txt\n",
                "root-secret.txt": "secret",
                "drop.log": "ignored",
                "keep.log": "shown",
                "locked/keep.txt": "still ignored because parent is ignored",
                "src/.gitignore": "*.tmp\n!keep.tmp\nprivate/\n",
                "src/drop.tmp": "ignored",
                "src/keep.tmp": "shown",
                "src/private/secret.txt": "ignored",
                "node_modules/pkg/index.js": "ignored by default",
                ".git/config": "ignored by default",
                ".project-preview-mcp/refresh-history.md": "service report, ignored by default",
            }
        )
        service = self.service()
        root_names = {entry["path"] for entry in service.browse()["entries"]}
        self.assertIn("keep.log", root_names)
        self.assertNotIn("drop.log", root_names)
        self.assertNotIn("root-secret.txt", root_names)
        self.assertNotIn("locked", root_names)
        self.assertNotIn("node_modules", root_names)
        self.assertNotIn(".git", root_names)
        self.assertNotIn(".project-preview-mcp", root_names)
        src_names = {entry["path"] for entry in service.browse(directory="src")["entries"]}
        self.assertIn("src/keep.tmp", src_names)
        self.assertNotIn("src/drop.tmp", src_names)
        self.assertNotIn("src/private", src_names)
        self.assertEqual(service.preview(path="locked/keep.txt")["error"], "ignored")
        self.assertEqual(service.preview(path=".project-preview-mcp/refresh-history.md")["error"], "ignored")

    def test_extra_exclusion_cannot_be_reenabled_by_gitignore(self) -> None:
        self.make_files({".gitignore": "!private.txt\n", "private.txt": "secret"})
        result = self.service("private.txt").preview(path="private.txt")
        self.assertEqual(result["error"], "ignored")

    def test_ignore_file_is_reloaded_between_calls(self) -> None:
        self.make_files({"item.txt": "visible"})
        service = self.service()
        self.assertTrue(service.preview(path="item.txt")["ok"])
        (self.root / ".gitignore").write_text("item.txt\n", encoding="utf-8")
        self.assertEqual(service.preview(path="item.txt")["error"], "ignored")

    def test_nested_path_and_ads_are_rejected(self) -> None:
        self.make_files({"safe.txt": "safe"})
        service = self.service()
        for path in ("../outside.txt", "C:/Windows/win.ini", "safe.txt:stream", "\\\\server\\share\\x"):
            with self.subTest(path=path):
                self.assertEqual(service.preview(path=path)["error"], "invalid_path")
        self.assertEqual(service.browse(directory="../")["error"], "invalid_path")

    @unittest.skipUnless(os.name == "nt", "Windows path aliases only")
    def test_windows_case_and_ambiguous_name_aliases_cannot_bypass_ignore(self) -> None:
        self.make_files({".gitignore": "secret.txt\n", "secret.txt": "hidden"})
        service = self.service()
        self.assertEqual(service.preview(path="SECRET.TXT")["error"], "ignored")
        self.assertEqual(service.preview(path="secret.txt.")["error"], "invalid_path")
        self.assertEqual(service.preview(path="NUL.txt")["error"], "invalid_path")

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases only")
    def test_windows_short_filename_alias_is_rejected(self) -> None:
        target = self.root / "secret-long-name.txt"
        target.write_text("hidden", encoding="utf-8")
        (self.root / ".gitignore").write_text("secret-long-name.txt\n", encoding="utf-8")
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(target), buffer, len(buffer))
        short_path = Path(buffer.value) if size else target
        short_relative = short_path.name
        if short_relative.casefold() == target.name.casefold():
            self.skipTest("8.3 short-name aliases are disabled on this volume")
        result = self.service().preview(path=short_relative)
        self.assertEqual(result["error"], "path_alias_disallowed")

    @unittest.skipUnless(os.name == "nt", "Windows symbolic link test")
    def test_windows_symbolic_link_escape_is_rejected_when_available(self) -> None:
        import shutil

        outside = Path(tempfile.mkdtemp())
        target = outside / "secret.txt"
        target.write_text("outside", encoding="utf-8")
        link = self.root / "escape-link"
        try:
            link.symlink_to(target)
        except OSError as exc:
            shutil.rmtree(outside, ignore_errors=True)
            self.skipTest(f"当前 Windows 环境不能创建符号链接：{exc}")
        try:
            self.assertEqual(self.service().preview(path="escape-link")["error"], "link_disallowed")
        finally:
            link.unlink(missing_ok=True)
            shutil.rmtree(outside, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_escape_and_cycle_are_rejected(self) -> None:
        import subprocess

        outside = Path(tempfile.mkdtemp())
        escape = self.root / "escape"
        cycle = self.root / "cycle"
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        env = os.environ.copy()
        env["PROJECT_PREVIEW_TEST_ESCAPE"] = str(escape)
        env["PROJECT_PREVIEW_TEST_OUTSIDE"] = str(outside)
        env["PROJECT_PREVIEW_TEST_CYCLE"] = str(cycle)
        command = (
            "New-Item -ItemType Junction -Path $env:PROJECT_PREVIEW_TEST_ESCAPE "
            "-Target $env:PROJECT_PREVIEW_TEST_OUTSIDE -ErrorAction Stop | Out-Null; "
            "New-Item -ItemType Junction -Path $env:PROJECT_PREVIEW_TEST_CYCLE "
            "-Target $env:PROJECT_PREVIEW_TEST_CYCLE_ROOT -ErrorAction Stop | Out-Null"
        )
        env["PROJECT_PREVIEW_TEST_CYCLE_ROOT"] = str(self.root)
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"无法创建 Windows junction：{completed.stderr.strip() or completed.stdout.strip()}")
            service = self.service()
            self.assertEqual(service.preview(path="escape/secret.txt")["error"], "link_disallowed")
            self.assertEqual(service.browse(directory="escape")["error"], "link_disallowed")
            self.assertEqual(service.browse(directory="cycle")["error"], "link_disallowed")
            self.assertNotIn("escape", {entry["path"] for entry in service.browse()["entries"]})
            self.assertNotIn("cycle", {entry["path"] for entry in service.browse()["entries"]})
        finally:
            import shutil

            for junction in (escape, cycle):
                try:
                    os.rmdir(junction)
                except OSError:
                    pass
            shutil.rmtree(outside, ignore_errors=True)

    def test_preview_bom_utf8_lines_and_file_end_has_no_next_line(self) -> None:
        self.make_files({"中文目录/示例.txt": b"\xef\xbb\xbf" + "中文\r\nsecond\n".encode("utf-8")})
        result = self.service().preview(path="中文目录/示例.txt", start_line=1, line_count=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines"], [{"line_number": 1, "content": "中文"}, {"line_number": 2, "content": "second"}])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["reason"], "file_end")
        self.assertIsNone(result["next_start_line"])
        self.assertIsNone(result["continuation"])

    def test_preview_error_types_are_distinct(self) -> None:
        self.make_files({"folder/child.txt": "child", "binary.bin": b"\x00\xff", "bad.txt": b"\xff", "plain.txt": "text"})
        service = self.service()
        self.assertEqual(service.preview(path="missing.txt")["error"], "not_found")
        self.assertEqual(service.preview(path="folder")["error"], "not_file")
        self.assertEqual(service.preview(path="binary.bin")["error"], "binary")
        self.assertEqual(service.preview(path="bad.txt")["error"], "unsupported_encoding")
        self.assertEqual(service.preview(path="plain.txt", start_line=0)["error"], "invalid_input")

    def test_unreadable_files_and_directories_return_unreadable(self) -> None:
        self.make_files({"private.txt": "text", "private-dir/child.txt": "child"})
        service = self.service()
        real_open = Path.open

        def deny_file_open(path: Path, *args: object, **kwargs: object):
            if path == self.root / "private.txt":
                raise PermissionError("controlled test denial")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", deny_file_open):
            self.assertEqual(service.preview(path="private.txt")["error"], "unreadable")

        with patch("project_preview.filesystem.os.scandir", side_effect=PermissionError("controlled test denial")):
            self.assertEqual(service.browse(directory="private-dir")["error"], "unreadable")

        with patch("project_preview.filesystem.os.scandir", side_effect=PermissionError("controlled test denial")):
            with self.assertRaises(ProjectSetupError):
                ProjectFiles(self.root)

    def test_long_line_is_not_returned_as_complete_or_resumable(self) -> None:
        self.make_files({"long.txt": "x" * 9_000 + "\nafter\n"})
        result = self.service().preview(path="long.txt")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reason"], "line_limit")
        self.assertEqual(result["lines"], [])
        self.assertIsNone(result["next_start_line"])
        self.assertIn("不提供按字节拆分", result["continuation"])

    def test_output_and_read_budgets_are_explicit(self) -> None:
        self.make_files({"many-long-lines.txt": ("x" * 7_000 + "\n") * 20})
        service = self.service()
        output_limited = service.preview(path="many-long-lines.txt", line_count=20)
        self.assertEqual(output_limited["reason"], "output_budget")
        self.assertTrue(output_limited["truncated"])
        self.assertEqual(output_limited["next_start_line"], len(output_limited["lines"]) + 1)
        self.assertLessEqual(service._json_size(output_limited), MAX_PREVIEW_OUTPUT_BYTES)

        payload = ("y" * 2_000 + "\n") * (MAX_PREVIEW_READ_BYTES // 2_000 + 20)
        self.make_files({"deep.txt": payload})
        read_limited = service.preview(path="deep.txt", start_line=600, line_count=1)
        self.assertEqual(read_limited["reason"], "read_budget")
        self.assertTrue(read_limited["truncated"])
        self.assertIsNone(read_limited["next_start_line"])
        self.assertTrue(read_limited["continuation"])

    def test_source_contents_are_unchanged_by_browse_and_preview(self) -> None:
        self.make_files({"src/文件.py": "print('hello')\n", "assets/pic.bin": b"\x00\x01"})
        before = self._snapshot()
        service = self.service()
        service.browse()
        service.preview(path="src/文件.py")
        after = self._snapshot()
        self.assertEqual(before, after)

    def test_path_search_uses_partial_relative_path_and_ignore_boundaries(self) -> None:
        self.make_files(
            {
                ".gitignore": "private/\n*.secret\n!visible.secret\n",
                "src/中文入口.py": "visible\n",
                "docs/中文说明.md": "docs\n",
                "private/中文隐藏.py": "hidden\n",
                "hidden.secret": "hidden\n",
                "visible.secret": "visible\n",
                "node_modules/dep/中文入口.py": "hidden\n",
            }
        )
        service = self.service()
        result = service.search(mode="path", query="中文")
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(
            [hit["path"] for hit in result["results"]],
            ["docs/中文说明.md", "src/中文入口.py"],
        )
        scoped = service.search(mode="path", query="中文", directory="src")
        self.assertEqual([hit["path"] for hit in scoped["results"]], ["src/中文入口.py"])
        no_results = service.search(mode="path", query="absent")
        self.assertEqual(no_results["outcome"], "no_results")
        self.assertFalse(no_results["truncated"])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is optional")
    def test_source_search_is_literal_handles_dash_and_returns_context(self) -> None:
        self.make_files(
            {
                ".gitignore": "private/\n",
                "src/入口.py": "上一行\nneedle [x] and -flag\n下一行\nneedle [x]\n",
                "private/hidden.py": "needle [x]\n",
                "node_modules/dep.py": "needle [x]\n",
            }
        )
        service = self.service()
        result = service.search(mode="source", query="needle [x]", directory="src", context_lines=1)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual([hit["line_number"] for hit in result["results"]], [2, 4])
        self.assertEqual(result["results"][0]["snippet"], "needle [x] and -flag")
        self.assertEqual(result["results"][0]["context"], [{"line_number": 1, "content": "上一行", "truncated": False}, {"line_number": 3, "content": "下一行", "truncated": False}])
        dash = service.search(mode="source", query="-flag", directory="src")
        self.assertEqual(dash["results"][0]["line_number"], 2)
        self.assertIn("-flag", dash["results"][0]["snippet"])
        absent = service.search(mode="source", query="not present")
        self.assertEqual(absent["outcome"], "no_results")

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is optional")
    def test_source_search_result_limit_and_output_budget_are_explicit(self) -> None:
        self.make_files({"many.txt": "\n".join(f"needle line {number}" for number in range(40))})
        service = self.service()
        limited = service.search(mode="source", query="needle", limit=3)
        self.assertEqual(limited["outcome"], "truncated")
        self.assertEqual(limited["reason"], "result_limit")
        self.assertEqual(len(limited["results"]), 3)
        self.assertLessEqual(service._json_size(limited), 48_000)

        with patch("project_preview.filesystem.MAX_SEARCH_OUTPUT_BYTES", 1_200):
            output_limited = service.search(mode="source", query="needle", limit=40, context_lines=0)
        self.assertEqual(output_limited["outcome"], "truncated")
        self.assertEqual(output_limited["reason"], "output_limit")
        self.assertLessEqual(service._json_size(output_limited), 1_200)

    def test_source_search_reports_missing_ripgrep_without_affecting_browse(self) -> None:
        self.make_files({"file.txt": "text\n"})
        service = self.service()
        with patch("project_preview.filesystem.shutil.which", return_value=None):
            result = service.search(mode="source", query="text")
        self.assertEqual(result["outcome"], "rg_unavailable")
        self.assertFalse(service.browse()["ok"] is False)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is optional")
    def test_source_search_distinguishes_timeout_and_execution_failure(self) -> None:
        self.make_files({"file.txt": "needle\n"})
        service = self.service()
        timeout = __import__("subprocess").TimeoutExpired("rg", 1, output=b"")
        with patch.object(ProjectFiles, "_run_rg_bounded", side_effect=timeout):
            timed_out = service.search(mode="source", query="needle")
        self.assertEqual(timed_out["outcome"], "timeout")
        self.assertTrue(timed_out["truncated"])

        with patch.object(ProjectFiles, "_run_rg_bounded", return_value=(2, b"", False)) as run:
            failed = service.search(mode="source", query="needle")
        self.assertEqual(failed["outcome"], "search_failed")
        command = run.call_args.args[0]
        self.assertIn("-e", command)
        self.assertIn("--", command)
        self.assertEqual(command[command.index("-e") + 1], "needle")

        with patch("project_preview.filesystem.subprocess.Popen") as popen:
            import io

            process = popen.return_value
            process.stdout = io.BytesIO()
            process.returncode = 1
            process.wait.return_value = 1
            ProjectFiles._run_rg_bounded(["rg", "-e", "-needle", "--", "file.txt"], cwd=self.root, timeout=1)
            self.assertFalse(popen.call_args.kwargs["shell"])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is optional")
    def test_source_search_skips_invalid_utf8_matches_consistently_with_preview(self) -> None:
        self.make_files({"bad.py": b"token invalid-\xff\n"})
        service = self.service()
        self.assertEqual(service.preview(path="bad.py")["error"], "unsupported_encoding")
        result = service.search(mode="source", query="token")
        self.assertEqual(result["outcome"], "truncated")
        self.assertEqual(result["reason"], "unsupported_encoding")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["skipped_files"], [{"path": "bad.py", "reason": "unsupported_encoding"}])
        self.assertNotIn("�", str(result))

    def test_search_validates_modes_queries_and_scope(self) -> None:
        self.make_files({"file.txt": "text\n"})
        service = self.service()
        self.assertEqual(service.search(mode="regex", query="x")["error"], "invalid_input")
        self.assertEqual(service.search(mode="source", query="")["error"], "invalid_input")
        self.assertEqual(service.search(mode="path", query="x", directory="../")["error"], "invalid_path")
        self.assertEqual(service.search(mode="path", query="x", directory="file.txt")["error"], "not_directory")

    def _snapshot(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                result[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result


if __name__ == "__main__":
    unittest.main()
