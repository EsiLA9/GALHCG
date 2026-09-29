from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from project_preview.filesystem import ProjectFiles
from project_preview.index import ProjectIndex
from project_preview.store import IndexStore, IndexedEntry, StoreError


class ProjectIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root_a = self.base / "项目 A"
        self.root_b = self.base / "项目 B"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.data = self.base / "service-data"
        self.store = IndexStore(self.data)
        self.projects = {
            "alpha": ProjectFiles(self.root_a),
            "beta": ProjectFiles(self.root_b),
        }
        for project_id, project in self.projects.items():
            self.store.register_project(project_id, project.root)
        self.index = ProjectIndex(self.projects, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_multi_project_refresh_isolated_and_persistent_without_source_contents(self) -> None:
        for root, body in ((self.root_a, "A private body marker"), (self.root_b, "B private body marker")):
            (root / "same.txt").write_text(body, encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

        for project_id in ("alpha", "beta"):
            result = self.index.refresh(project_id)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["discovered_file_count"], 2)

        alpha = self.index.list_files("alpha", limit=1)
        beta = self.index.list_files("beta", limit=10)
        self.assertEqual(alpha["total"], 2)
        self.assertEqual(alpha["next_offset"], 1)
        self.assertEqual(beta["total"], 2)
        self.assertEqual([row["path"] for row in beta["entries"]], ["same.txt", "src/main.py"])
        self.assertEqual(self.index.list_files("alpha")["entries"][0]["path"], "same.txt")

        connection = sqlite3.connect(self.store.path)
        try:
            schema = connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            data = self.store.path.read_bytes()
        finally:
            connection.close()
            self.assertEqual(schema, 7)
        self.assertIn("files", tables)
        self.assertIn("map_nodes", tables)
        self.assertNotIn(b"A private body marker", data)
        self.assertNotIn(b"B private body marker", data)

        reopened = IndexStore(self.data)
        self.assertEqual(reopened.list_files("alpha")["total"], 2)
        self.assertEqual(reopened.status("alpha")["projects"][0]["discovered_file_count"], 2)

    def test_scoped_refresh_prunes_only_its_scope_and_handles_path_type_changes(self) -> None:
        (self.root_a / "src").mkdir()
        (self.root_a / "docs").mkdir()
        (self.root_a / "src" / "old.py").write_text("old", encoding="utf-8")
        (self.root_a / "docs" / "keep.md").write_text("keep", encoding="utf-8")
        self.index.refresh("alpha")

        (self.root_a / "src" / "old.py").unlink()
        (self.root_a / "src" / "new.py").write_text("new", encoding="utf-8")
        scoped = self.index.refresh("alpha", "src")
        self.assertTrue(scoped["ok"], scoped)
        paths = [entry["path"] for entry in self.index.list_files("alpha")["entries"]]
        self.assertEqual(paths, ["docs/keep.md", "src/new.py"])

        # A file replaces a tracked directory: descendants disappear and the type changes.
        (self.root_a / "src" / "new.py").unlink()
        (self.root_a / "src").rmdir()
        (self.root_a / "src").write_text("now a file", encoding="utf-8")
        full = self.index.refresh("alpha")
        self.assertTrue(full["ok"], full)
        with_directories = self.index.list_files("alpha", include_directories=True)
        src = [entry for entry in with_directories["entries"] if entry["path"] == "src"]
        self.assertEqual(src[0]["type"], "file")
        self.assertEqual([entry["path"] for entry in with_directories["entries"]], ["docs", "docs/keep.md", "src"])

    def test_failed_scan_is_atomic_and_status_reports_failed_scope(self) -> None:
        (self.root_a / "src").mkdir()
        (self.root_a / "src" / "before.py").write_text("before", encoding="utf-8")
        self.assertTrue(self.index.refresh("alpha")["ok"])
        before = self.index.list_files("alpha", include_directories=True)["entries"]

        real_scandir = os.scandir

        def fail_subdirectory(path):
            if Path(path) == self.root_a / "src":
                raise PermissionError("controlled scan failure")
            return real_scandir(path)

        with patch("project_preview.filesystem.os.scandir", side_effect=fail_subdirectory):
            failed = self.index.refresh("alpha")
        self.assertFalse(failed["ok"], failed)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.index.list_files("alpha", include_directories=True)["entries"], before)
        status = self.index.status("alpha")["projects"][0]
        self.assertEqual(status["latest_refresh"]["status"], "failed")
        self.assertEqual(status["latest_refresh"]["scope"], "")
        self.assertEqual(status["last_successful_refresh"]["scope"], "")
        history = self.index.refresh_history("alpha")
        self.assertTrue(history["ok"], history)
        failed_run = next(run for run in history["runs"] if run["refresh_id"] == failed["refresh_id"])
        self.assertEqual(failed_run["status"], "failed")
        self.assertEqual(failed_run["error_code"], failed["error"])
        self.assertGreaterEqual(failed_run["scan_duration_ms"], 0)

    def test_refresh_history_persists_metrics_and_paginates(self) -> None:
        source = self.root_a / "sample.txt"
        source.write_text("12345", encoding="utf-8")
        first = self.index.refresh("alpha")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["indexed_bytes"], 5)
        self.assertGreaterEqual(first["scan_duration_ms"], 0)

        second = self.index.refresh("alpha")
        self.assertTrue(second["ok"], second)
        source.unlink()
        third = self.index.refresh("alpha")
        self.assertTrue(third["ok"], third)
        self.assertEqual(third["removed_entry_count"], 1)

        page1 = self.index.refresh_history("alpha", limit=2, offset=0)
        page2 = self.index.refresh_history("alpha", limit=2, offset=2)
        self.assertTrue(page1["ok"], page1)
        self.assertTrue(page2["ok"], page2)
        self.assertEqual(page1["total"], 3)
        self.assertEqual(len(page1["runs"]), 2)
        self.assertEqual(page1["next_offset"], 2)
        self.assertEqual(len(page2["runs"]), 1)
        runs = {run["refresh_id"]: run for run in page1["runs"] + page2["runs"]}
        self.assertEqual(runs[first["refresh_id"]]["indexed_bytes"], 5)
        self.assertIsInstance(runs[first["refresh_id"]]["elapsed_ms"], int)
        self.assertEqual(runs[third["refresh_id"]]["removed_entry_count"], 1)
        self.assertIsNone(self.index.refresh_history("beta")["next_offset"])

        reopened = IndexStore(self.data)
        self.assertEqual(reopened.refresh_history("alpha")["total"], 3)

    def test_incomplete_run_is_visible_and_does_not_change_existing_manifest(self) -> None:
        (self.root_a / "stable.txt").write_text("stable", encoding="utf-8")
        self.index.refresh("alpha")
        before = self.index.list_files("alpha")["entries"]
        self.store.start_refresh("alpha", "src")
        state = self.index.status("alpha")["projects"][0]
        self.assertTrue(state["refresh_incomplete"])
        self.assertEqual(state["latest_refresh"]["status"], "running")
        self.assertEqual(self.index.list_files("alpha")["entries"], before)

    def test_project_id_is_bound_to_a_root_and_datadir_must_be_absolute(self) -> None:
        other_root = self.base / "other"
        other_root.mkdir()
        with self.assertRaises(StoreError):
            self.store.register_project("alpha", other_root)
        with self.assertRaises(StoreError):
            IndexStore(Path("relative-data-dir"))
        status = {item["project_id"]: item for item in self.index.status()["projects"]}
        self.assertEqual(len(status["alpha"]["root_fingerprint"]), 16)
        self.assertNotEqual(status["alpha"]["root_fingerprint"], status["beta"]["root_fingerprint"])
        self.assertEqual(status["alpha"]["semantic_map"]["freshness"]["known_stale_owner_count"], 0)
        self.assertEqual(status["alpha"]["semantic_map"]["file_manifest_count"], 0)
        common_prefix = "C:/long-project/" + "x" * 520
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE projects SET root=?, root_key=? WHERE project_id='alpha'",
                (common_prefix + "A", common_prefix + "A"),
            )
            connection.execute(
                "UPDATE projects SET root=?, root_key=? WHERE project_id='beta'",
                (common_prefix + "B", common_prefix + "B"),
            )
        long_roots = {item["project_id"]: item for item in self.index.status()["projects"]}
        self.assertTrue(long_roots["alpha"]["root_truncated"])
        self.assertTrue(long_roots["beta"]["root_truncated"])
        self.assertEqual(long_roots["alpha"]["root"], long_roots["beta"]["root"])
        self.assertNotEqual(long_roots["alpha"]["root_fingerprint"], long_roots["beta"]["root_fingerprint"])

    def test_status_hides_projects_not_in_current_server_configuration(self) -> None:
        retired_root = self.base / "retired"
        retired_root.mkdir()
        self.store.register_project("retired", retired_root)
        result = self.index.status()
        self.assertEqual(result["configured_project_count"], 2)
        self.assertEqual({item["project_id"] for item in result["projects"]}, {"alpha", "beta"})

    def test_list_files_json_budget_returns_short_page_and_next_offset(self) -> None:
        refresh_id = self.store.start_refresh("alpha", "")
        entries = [
            IndexedEntry(f"{number:03d}-{'x' * 150}.txt", "file", 1, 1)
            for number in range(450)
        ]
        self.store.complete_refresh(refresh_id, "alpha", "", entries)
        result = self.index.list_files("alpha", limit=500)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(size, 48_000)
        self.assertTrue(result["output_limited"])
        self.assertEqual(result["reason"], "output_budget")
        self.assertGreater(result["next_offset"], 0)
        second = self.index.list_files("alpha", limit=500, offset=result["next_offset"])
        self.assertNotEqual(result["entries"][0]["path"], second["entries"][0]["path"])

    def test_status_has_stable_paging_for_many_configured_projects(self) -> None:
        extra: dict[str, ProjectFiles] = {}
        for number in range(23):
            project_id = f"extra{number:02d}"
            root = self.base / project_id
            root.mkdir()
            project = ProjectFiles(root)
            self.store.register_project(project_id, root)
            extra[project_id] = project
        index = ProjectIndex({**self.projects, **extra}, self.store)
        first = index.status(limit=10)
        second = index.status(limit=10, offset=10)
        self.assertEqual(first["total"], 25)
        self.assertEqual(len(first["projects"]), 10)
        self.assertEqual(first["next_offset"], 10)
        self.assertLess(first["projects"][-1]["project_id"], second["projects"][0]["project_id"])
        self.assertLessEqual(
            len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            48_000,
        )

    def test_same_project_refreshes_are_serialized_within_process(self) -> None:
        project = self.projects["alpha"]
        real_scan = project.scan_manifest
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def observed_scan(directory: str = ""):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            try:
                return real_scan(directory)
            finally:
                with counter_lock:
                    active -= 1

        project.scan_manifest = observed_scan  # type: ignore[method-assign]
        results: list[dict[str, object]] = []
        workers = [threading.Thread(target=lambda: results.append(self.index.refresh("alpha"))) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(max_active, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))

    def test_list_files_scope_validation_and_directory_visibility(self) -> None:
        (self.root_a / "src").mkdir()
        (self.root_a / "src" / "a.py").write_text("x", encoding="utf-8")
        self.index.refresh("alpha")
        self.assertEqual(self.index.list_files("alpha", directory="../outside")["error"], "invalid_path")
        files = self.index.list_files("alpha", directory="src")
        self.assertEqual([entry["path"] for entry in files["entries"]], ["src/a.py"])
        entries = self.index.list_files("alpha", directory="src", include_directories=True)
        self.assertEqual([entry["path"] for entry in entries["entries"]], ["src", "src/a.py"])


if __name__ == "__main__":
    unittest.main()
