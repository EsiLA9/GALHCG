from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_preview.filesystem import ProjectFiles
from project_preview.export_map_markdown import read_graph_snapshot, render_graph_markdown
from project_preview.index import ProjectIndex
from project_preview.store import IndexStore, StoreError
from project_preview.versions import SnapshotResult, VersionResult


class SemanticMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root_a = self.base / "alpha"
        self.root_b = self.base / "beta"
        self.root_a.mkdir()
        self.root_b.mkdir()
        for root, content in ((self.root_a, "def login():\n    return 'ok'\n"), (self.root_b, "def login():\n    return 'beta'\n")):
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text(content, encoding="utf-8")
        self.store = IndexStore(self.base / "service-data")
        self.projects = {"alpha": ProjectFiles(self.root_a), "beta": ProjectFiles(self.root_b)}
        for project_id, project in self.projects.items():
            self.store.register_project(project_id, project.root)
        self.index = ProjectIndex(self.projects, self.store)
        for project_id in self.projects:
            self.assertTrue(self.index.refresh(project_id)["ok"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def file_id(self, project_id: str, path: str = "src/auth.py") -> str:
        rows = self.index.list_files(project_id, include_directories=True)["entries"]
        return next(row["node_id"] for row in rows if row["path"] == path)

    def token(self, project_id: str = "alpha", path: str = "src/auth.py") -> str:
        result = self.index.preview(project_id, path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["version_token_status"], "available", result)
        return result["version_token"]

    @staticmethod
    def semantic_node(node_id: str, node_type: str, name: str, *, state: str = "tentative", evidence=None, **extra):
        return {
            "id": node_id,
            "type": node_type,
            "name": name,
            "summary": extra.get("summary", ""),
            "aliases": extra.get("aliases", []),
            "state": state,
            "evidence": evidence or [],
        }

    def test_confirmed_node_and_edge_evidence_context_and_map_search(self) -> None:
        token = self.token()
        file_id = self.file_id("alpha")
        result = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:login", "Concept", "Authentication entry", state="confirmed",
                summary="Checks a credential and returns a session.", aliases=["sign in", "credentials"],
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
            upsert_edges=[{
                "source_id": "concept:login", "relation": "maps_to", "target_id": file_id,
                "evidence": [{"path": "src/auth.py", "version_token": token}],
            }],
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["upserted_node_ids"], ["concept:login"])
        context = self.index.context("alpha", "concept:login")
        self.assertTrue(context["ok"], context)
        self.assertEqual(context["node"]["state"], "confirmed")
        self.assertEqual(context["node_freshness"]["status"], "fresh")
        self.assertTrue(context["completeness"]["node_evidence"]["complete"])
        self.assertEqual(context["evidence"][0]["path"], "src/auth.py")
        self.assertEqual(context["files"][0]["path"], "src/auth.py")
        self.assertTrue(any(item["relation"] == "maps_to" and item["direction"] == "outgoing" for item in context["neighbors"]))
        self.assertEqual(context["neighbors"][0]["evidence"][0]["path"], "src/auth.py")
        self.assertEqual(context["neighbors"][0]["roles"], ["unspecified"])

        for query in ("Authentication", "credentials", "session"):
            found = self.index.search_map("alpha", query)
            self.assertTrue(found["ok"], found)
            self.assertEqual(found["results"][0]["id"], "concept:login")
            self.assertEqual(found["results"][0]["project_id"], "alpha")
        self.assertIn("summary", self.index.search_map("alpha", "session")["results"][0]["matched_fields"])
        concept_only = self.index.search_map("alpha", "Authentication", node_types=["Concept"])
        self.assertEqual(concept_only["results"][0]["matched_fields"], ["name"])

        reopened_store = IndexStore(self.base / "service-data")
        reopened_store.register_project("alpha", self.root_a)
        reopened = ProjectIndex({"alpha": ProjectFiles(self.root_a)}, reopened_store)
        self.assertEqual(reopened.context("alpha", "concept:login")["evidence"][0]["path"], "src/auth.py")
        self.assertEqual(reopened.search_map("alpha", "credentials")["results"][0]["id"], "concept:login")

    def test_refresh_file_deletion_cascades_edges_but_keeps_historical_evidence(self) -> None:
        token = self.token()
        file_id = self.file_id("alpha")
        created = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:login", "Concept", "Login", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
            upsert_edges=[{"source_id": "concept:login", "relation": "maps_to", "target_id": file_id}],
        )
        self.assertTrue(created["ok"], created)
        (self.root_a / "src" / "auth.py").unlink()
        stale_before_refresh = self.index.context("alpha", "concept:login")
        self.assertEqual(stale_before_refresh["node_freshness"]["status"], "stale")
        self.assertEqual(stale_before_refresh["node_freshness"]["files"][0]["reason"], "not_found")
        refreshed = self.index.refresh("alpha", "src")
        self.assertTrue(refreshed["ok"], refreshed)
        self.assertGreaterEqual(
            self.index.status("alpha")["projects"][0]["semantic_map"]["freshness"]["known_stale_owner_count"], 1
        )

        context = self.index.context("alpha", "concept:login")
        self.assertTrue(context["ok"], context)
        self.assertEqual(context["node"]["state"], "confirmed")
        self.assertEqual(context["evidence"][0]["path"], "src/auth.py")
        self.assertEqual(context["files"][0]["type"], "missing_file")
        self.assertEqual(context["node_freshness"]["status"], "stale")
        self.assertFalse(any(neighbor["relation"] == "maps_to" for neighbor in context["neighbors"]))
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM node_evidence").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM edge_evidence").fetchone()[0], 0)

    def test_stale_same_size_file_with_restored_mtime_rejects_entire_batch(self) -> None:
        token = self.token()
        source = self.root_a / "src" / "auth.py"
        info = source.stat()
        original = source.read_bytes()
        changed = original.replace(b"login", b"logon")
        self.assertEqual(len(changed), len(original))
        source.write_bytes(changed)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        result = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "module:auth", "Module", "Auth", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], "stale_evidence")
        self.assertEqual(result["stale_files"], ["src/auth.py"])
        self.assertEqual(self.index.search_map("alpha", "Auth")["total"], 0)

    def test_preview_token_matches_snapshot_when_file_changes_and_is_restored_during_read(self) -> None:
        project = self.projects["alpha"]
        original_preview = project.preview
        source = self.root_a / "src" / "auth.py"
        original_bytes = source.read_bytes()

        def change_after_read(**kwargs):
            old_info = source.stat()
            changed = original_bytes.replace(b"login", b"logon")
            source.write_bytes(changed)
            os.utime(source, ns=(old_info.st_atime_ns, old_info.st_mtime_ns))
            try:
                return original_preview(**kwargs)
            finally:
                source.write_bytes(original_bytes)
                os.utime(source, ns=(old_info.st_atime_ns, old_info.st_mtime_ns))

        with patch.object(project, "preview", side_effect=change_after_read):
            result = self.index.preview("alpha", "src/auth.py")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["version_token_status"], "available")
        self.assertEqual([line["content"] for line in result["lines"]], original_bytes.decode().splitlines())
        accepted = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:snapshot", "Concept", "Snapshot", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": result["version_token"]}],
            )],
        )
        self.assertTrue(accepted["ok"], accepted)

    def test_failed_batch_and_relation_validation_are_atomic(self) -> None:
        file_id = self.file_id("alpha")
        token = self.token()
        valid_node = self.semantic_node(
            "concept:valid", "Concept", "Valid", state="confirmed",
            evidence=[{"path": "src/auth.py", "version_token": token}],
        )
        missing = self.index.update_map(
            "alpha",
            upsert_nodes=[valid_node],
            upsert_edges=[{"source_id": "concept:valid", "relation": "maps_to", "target_id": "file:missing"}],
        )
        self.assertEqual(missing["error"], "missing_endpoint")
        self.assertEqual(self.index.search_map("alpha", "Valid")["total"], 0)

        invalid = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node("module:auth", "Module", "Auth")],
            upsert_edges=[{"source_id": "module:auth", "relation": "maps_to", "target_id": file_id}],
        )
        self.assertEqual(invalid["error"], "invalid_edge_types")
        self.assertEqual(self.index.search_map("alpha", "Auth")["total"], 0)

        duplicate = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node("concept:a", "Concept", "A"),
                self.semantic_node("concept:b", "Concept", "B"),
            ],
            upsert_edges=[
                {"source_id": "concept:a", "relation": "related_to", "target_id": "concept:b"},
                {"source_id": "concept:b", "relation": "related_to", "target_id": "concept:a"},
            ],
        )
        self.assertEqual(duplicate["error"], "duplicate_edge")
        self.assertEqual(self.index.search_map("alpha", "A")["total"], 0)

        cycle = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node("module:a", "Module", "Module A"),
                self.semantic_node("module:b", "Module", "Module B"),
            ],
            upsert_edges=[
                {"source_id": "module:a", "relation": "contains", "target_id": "module:b"},
                {"source_id": "module:b", "relation": "contains", "target_id": "module:a"},
            ],
        )
        self.assertEqual(cycle["error"], "contains_cycle")
        self.assertEqual(self.index.search_map("alpha", "Module A")["total"], 0)

    def test_same_name_ids_multiple_file_mapping_and_semantic_delete_cascades(self) -> None:
        second_file = self.root_a / "src" / "token.py"
        second_file.write_text("def issue_token():\n    return 'token'\n", encoding="utf-8")
        self.assertTrue(self.index.refresh("alpha", "src")["ok"])
        first_token = self.token("alpha", "src/auth.py")
        second_token = self.token("alpha", "src/token.py")
        first_file_id = self.file_id("alpha", "src/auth.py")
        second_file_id = self.file_id("alpha", "src/token.py")
        result = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node(
                    "concept:auth-login", "Concept", "Authentication", state="confirmed",
                    evidence=[{"path": "src/auth.py", "version_token": first_token}],
                ),
                self.semantic_node(
                    "concept:token-login", "Concept", "Authentication", state="confirmed",
                    evidence=[{"path": "src/token.py", "version_token": second_token}],
                ),
            ],
            upsert_edges=[
                {
                    "source_id": "concept:auth-login", "relation": "maps_to", "target_id": first_file_id,
                    "evidence": [{"path": "src/auth.py", "version_token": first_token}],
                },
                {
                    "source_id": "concept:auth-login", "relation": "maps_to", "target_id": second_file_id,
                    "evidence": [{"path": "src/token.py", "version_token": second_token}],
                },
            ],
        )
        self.assertTrue(result["ok"], result)
        matches = self.index.search_map("alpha", "Authentication")
        self.assertEqual(matches["total"], 2)
        self.assertEqual({item["id"] for item in matches["results"]}, {"concept:auth-login", "concept:token-login"})
        multi_context = self.index.context("alpha", "concept:auth-login")
        self.assertEqual({item["path"] for item in multi_context["files"]}, {"src/auth.py", "src/token.py"})

        deleted_edge = self.index.update_map(
            "alpha",
            delete_edges=[{
                "source_id": "concept:auth-login", "relation": "maps_to", "target_id": second_file_id
            }],
        )
        self.assertTrue(deleted_edge["ok"], deleted_edge)
        with self.store._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM edge_evidence WHERE project_id='alpha'").fetchone()[0], 1
            )

        deleted_node = self.index.update_map("alpha", delete_node_ids=["concept:auth-login"])
        self.assertTrue(deleted_node["ok"], deleted_node)
        self.assertEqual(self.index.context("alpha", "concept:auth-login")["error"], "node_not_found")
        with self.store._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM node_evidence WHERE project_id='alpha' AND node_id='concept:auth-login'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM map_edges WHERE project_id='alpha' AND source_id='concept:auth-login'").fetchone()[0],
                0,
            )

    def test_project_scoped_ids_tokens_and_cross_project_edges_are_isolated(self) -> None:
        alpha_file = self.file_id("alpha")
        beta_file = self.file_id("beta")
        alpha_token = self.token("alpha")
        # Same user node IDs are allowed in separate project namespaces.
        for project_id, token in (("alpha", alpha_token), ("beta", self.token("beta"))):
            result = self.index.update_map(
                project_id,
                upsert_nodes=[self.semantic_node("concept:entry", "Concept", "Entry", state="confirmed",
                                                  evidence=[{"path": "src/auth.py", "version_token": token}])],
            )
            self.assertTrue(result["ok"], result)
        self.assertTrue(self.index.update_map(
            "beta", upsert_nodes=[self.semantic_node("concept:beta-only", "Concept", "Beta only")]
        )["ok"])
        alpha_context = self.index.context("alpha", "concept:entry")
        beta_context = self.index.context("beta", "concept:entry")
        self.assertEqual(alpha_context["project_id"], "alpha")
        self.assertEqual(beta_context["project_id"], "beta")
        self.assertNotEqual(alpha_file, beta_file)

        cross = self.index.update_map(
            "alpha",
            upsert_edges=[{"source_id": "concept:entry", "relation": "related_to", "target_id": "concept:beta-only"}],
        )
        self.assertEqual(cross["error"], "cross_project_edge")

        wrong_project_token = self.index.update_map(
            "beta",
            upsert_nodes=[self.semantic_node("concept:wrong", "Concept", "Wrong", state="confirmed",
                                              evidence=[{"path": "src/auth.py", "version_token": alpha_token}])],
        )
        self.assertEqual(wrong_project_token["error"], "stale_evidence")

    def test_version_hash_budget_returns_successful_preview_without_token(self) -> None:
        unavailable = SnapshotResult(None, VersionResult(None, "file_too_large", 0))
        with patch("project_preview.filesystem.read_versioned_snapshot", return_value=unavailable):
            result = self.index.preview("alpha", "src/auth.py")
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["version_token"])
        self.assertEqual(result["version_token_reason"], "file_too_large")

    def test_update_map_maximum_id_response_stays_under_tool_budget(self) -> None:
        old_ids = ["concept:" + str(number).zfill(3) + "d" * 117 for number in range(50)]
        created = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(node_id, "Concept", "Old concept") for node_id in old_ids],
        )
        self.assertTrue(created["ok"], created)
        new_ids = ["module:" + str(number).zfill(3) + "u" * 118 for number in range(50)]
        result = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(node_id, "Module", "New module") for node_id in new_ids],
            delete_node_ids=old_ids,
        )
        self.assertTrue(result["ok"], result)
        payload_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(payload_size, 48_000)

    def test_concept_dag_context_reports_limits_and_traverse_preserves_direction(self) -> None:
        nodes = [
            self.semantic_node("concept:system", "Concept", "System"),
            self.semantic_node("concept:management", "Concept", "Management"),
            self.semantic_node("concept:mechanism", "Concept", "Mechanism"),
            self.semantic_node("concept:shared", "Concept", "Shared mechanism"),
        ]
        edges = [
            {"source_id": "concept:system", "relation": "contains", "target_id": "concept:management"},
            {"source_id": "concept:management", "relation": "contains", "target_id": "concept:mechanism"},
            {"source_id": "concept:mechanism", "relation": "contains", "target_id": "concept:shared"},
            {"source_id": "concept:management", "relation": "contains", "target_id": "concept:shared"},
            {"source_id": "concept:system", "relation": "depends_on", "target_id": "concept:management"},
            {"source_id": "concept:management", "relation": "depends_on", "target_id": "concept:system"},
        ]
        for number in range(21):
            node_id = f"concept:parent-{number:02d}"
            nodes.append(self.semantic_node(node_id, "Concept", f"Parent {number:02d}"))
            edges.append({"source_id": node_id, "relation": "contains", "target_id": "concept:mechanism"})
        saved = self.index.update_map("alpha", upsert_nodes=nodes, upsert_edges=edges)
        self.assertTrue(saved["ok"], saved)

        empty_page = self.index.context("alpha", "concept:mechanism", neighbor_limit=0)
        self.assertEqual(empty_page["structure"]["parents"], [])
        self.assertEqual(empty_page["completeness"]["parents"]["total"], 22)
        self.assertFalse(empty_page["completeness"]["parents"]["complete"])
        self.assertEqual(empty_page["completeness"]["parents"]["continue_with"], "traverse")

        ancestors = self.index.context("alpha", "concept:shared", neighbor_limit=20)
        ancestor_ids = {item["id"] for item in ancestors["structure"]["ancestors"]}
        parent_ids = {item["id"] for item in ancestors["structure"]["parents"]}
        self.assertIn("concept:system", ancestor_ids)
        self.assertIn("concept:management", ancestor_ids)
        self.assertIn("concept:mechanism", ancestor_ids)
        self.assertEqual(parent_ids, {"concept:mechanism", "concept:management"})

        path = self.index.traverse(
            "alpha", "concept:system", target_node_id="concept:shared", max_depth=4,
            node_limit=2, edge_limit=2,
        )
        self.assertTrue(path["target_reached"], path)
        self.assertEqual(len(path["shortest_path"]), 2)
        self.assertTrue(path["next_cursor"])
        next_page = self.index.traverse(
            "alpha", "concept:system", target_node_id="concept:shared", max_depth=4,
            node_limit=2, edge_limit=2, cursor=path["next_cursor"],
        )
        self.assertTrue(next_page["ok"], next_page)
        self.assertEqual(next_page["node_offset"], 2)
        self.assertEqual(next_page["edge_offset"], 2)
        self.assertTrue(all("source_id" in edge and "target_id" in edge for edge in next_page["edges"]))
        paged_nodes = [*path["nodes"], *next_page["nodes"]]
        paged_edges = [*path["edges"], *next_page["edges"]]
        self.assertEqual(len({node["node_id"] for node in paged_nodes}), path["total_nodes"])
        self.assertEqual(len({edge["edge_id"] for edge in paged_edges}), path["total_edges"])

        incoming = self.index.traverse(
            "alpha", "concept:shared", relations=["contains"], direction="incoming", max_depth=1,
        )
        self.assertTrue(all(edge["traversal_direction"] == "incoming" for edge in incoming["edges"]))
        self.assertTrue(all(edge["source_id"] != "concept:shared" for edge in incoming["edges"]))
        self.assertFalse(incoming["complete"])
        self.assertEqual(incoming["stop_reason"], "depth_limit")

        changed = self.index.update_map(
            "alpha", upsert_nodes=[self.semantic_node("concept:revision", "Concept", "Revision")]
        )
        self.assertTrue(changed["ok"], changed)
        isolated = self.index.traverse("alpha", "concept:revision")
        self.assertTrue(isolated["complete"])
        self.assertEqual(isolated["total_nodes"], 1)
        stale_cursor = self.index.traverse(
            "alpha", "concept:system", target_node_id="concept:shared", max_depth=4,
            node_limit=2, edge_limit=2, cursor=path["next_cursor"],
        )
        self.assertEqual(stale_cursor["error"], "stale_cursor")
        self.assertTrue(stale_cursor["restart_required"])
        foreign_cursor = self.index.traverse(
            "beta", "concept:system", target_node_id="concept:shared", max_depth=4,
            node_limit=2, edge_limit=2, cursor=path["next_cursor"],
        )
        self.assertEqual(foreign_cursor["error"], "invalid_cursor")
        cyclic = self.index.update_map("alpha", upsert_edges=[{
            "source_id": "concept:shared", "relation": "contains", "target_id": "concept:system",
        }])
        self.assertEqual(cyclic["error"], "contains_cycle")
        deleted_parent = self.index.update_map("alpha", delete_node_ids=["concept:management"])
        self.assertTrue(deleted_parent["ok"], deleted_parent)
        self.assertEqual(self.index.context("alpha", "concept:management")["error"], "node_not_found")
        self.assertTrue(self.index.context("alpha", "concept:shared")["ok"])

    def test_traverse_distinguishes_work_budgets_and_output_truncation(self) -> None:
        nodes = [self.semantic_node(
            f"concept:budget-{number:02d}", "Concept", f"Budget {number:02d}", summary="x" * 500,
        ) for number in range(12)]
        edges = [{
            "source_id": f"concept:budget-{number:02d}", "relation": "contains",
            "target_id": f"concept:budget-{number + 1:02d}",
        } for number in range(11)]
        saved = self.index.update_map("alpha", upsert_nodes=nodes, upsert_edges=edges)
        self.assertTrue(saved["ok"], saved)
        deep_context = self.index.context("alpha", "concept:budget-11")
        self.assertFalse(deep_context["completeness"]["ancestors"]["complete"])
        self.assertEqual(deep_context["completeness"]["ancestors"]["continue_with"], "traverse")

        with patch("project_preview.semantic_map.MAX_TRAVERSE_NODES", 2):
            node_limited = self.index.traverse("alpha", "concept:budget-00", max_depth=5)
        self.assertEqual(node_limited["stop_reason"], "node_budget")
        self.assertFalse(node_limited["complete"])

        with patch("project_preview.semantic_map.MAX_TRAVERSE_EDGES", 1):
            edge_limited = self.index.traverse("alpha", "concept:budget-00", max_depth=5)
        self.assertEqual(edge_limited["stop_reason"], "edge_budget")

        with patch("project_preview.semantic_map.MAX_TRAVERSE_SECONDS", 0.0):
            time_limited = self.index.traverse("alpha", "concept:budget-00", max_depth=5)
        self.assertEqual(time_limited["stop_reason"], "time_budget")

        with patch("project_preview.semantic_map.MAX_MAP_OUTPUT_BYTES", 4_000):
            output_limited = self.index.traverse("alpha", "concept:budget-00", max_depth=8)
        self.assertTrue(output_limited["output_limited"])
        self.assertLessEqual(
            len(json.dumps(output_limited, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            4_000,
        )

    def test_dry_run_path_resolution_map_roles_and_stale_evidence(self) -> None:
        (self.root_a / "src" / "Affector.py").write_text("placeholder\n", encoding="utf-8")
        self.assertTrue(self.index.refresh("alpha", "src")["ok"])
        token = self.token()
        file_id = self.file_id("alpha")
        changes = {
            "upsert_nodes": [self.semantic_node(
                "concept:dry", "Concept", "Affector", aliases=["condition freshness"],
                state="confirmed", evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
            "upsert_edges": [{
                "source_id": "concept:dry", "relation": "maps_to", "target_id": file_id,
                "roles": ["implementation", "test"],
                "evidence": [{"path": "src/auth.py", "version_token": token}],
            }],
        }
        before = self.index.search_map("alpha", "Affector")["total"]
        preview = self.index.update_map("alpha", **changes, dry_run=True)
        self.assertTrue(preview["valid"], preview)
        self.assertEqual(preview["evidence_references"], 2)
        self.assertEqual(preview["unique_paths"], 1)
        self.assertEqual(self.index.search_map("alpha", "Affector")["total"], before)

        mapped = self.index.update_map("alpha", **changes)
        self.assertTrue(mapped["ok"], mapped)
        context = self.index.context("alpha", "concept:dry")
        map_edge = next(edge for edge in context["neighbors"] if edge["relation"] == "maps_to")
        self.assertEqual(map_edge["roles"], ["implementation", "test"])
        self.assertEqual(self.index.search_map("alpha", "affector")["total"], 0)
        self.assertEqual(self.index.search_map("alpha", "Affector")["total"], 2)
        self.assertEqual(self.index.search_map("alpha", "Affector", node_types=["Concept"])["total"], 1)
        filtered = self.index.search_map("alpha", "affector", case_sensitive=False, node_types=["Concept"])
        self.assertEqual(filtered["results"][0]["matched_fields"], ["name"])
        alias_match = self.index.search_map("alpha", "condition freshness", case_sensitive=False, node_types=["Concept"])
        self.assertEqual(alias_match["results"][0]["matched_fields"], ["aliases"])

        resolved = self.index.resolve_paths("alpha", ["src/auth.py"])
        self.assertEqual(resolved["results"][0]["node_id"], file_id)
        other_resolved = self.index.resolve_paths("beta", ["src/auth.py"])
        self.assertNotEqual(other_resolved["results"][0]["node_id"], file_id)
        documentation = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node("concept:docs", "Concept", "Authentication documentation")],
            upsert_edges=[{
                "source_id": "concept:docs", "relation": "maps_to", "target_id": file_id,
                "roles": ["documentation"],
            }],
        )
        self.assertTrue(documentation["ok"], documentation)
        doc_context = self.index.context("alpha", "concept:docs")
        self.assertEqual(doc_context["neighbors"][0]["roles"], ["documentation"])
        fresh_file = self.root_a / "src" / "new.py"
        fresh_file.write_text("new\n", encoding="utf-8")
        unindexed = self.index.resolve_paths("alpha", ["src/new.py"])
        self.assertTrue(unindexed["results"][0]["refresh_required"])
        self.assertIsNone(unindexed["results"][0]["node_id"])

        source = self.root_a / "src" / "auth.py"
        unrelated_source = self.root_a / "src" / "other.py"
        unrelated_source.write_text("independent evidence\n", encoding="utf-8")
        self.assertTrue(self.index.refresh("alpha", "src")["ok"])
        unrelated_token = self.token("alpha", "src/other.py")
        unrelated = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:unrelated", "Concept", "Unrelated", state="confirmed",
                evidence=[{"path": "src/other.py", "version_token": unrelated_token}],
            )],
        )
        self.assertTrue(unrelated["ok"], unrelated)
        original = source.read_bytes()
        stat = source.stat()
        source.write_bytes(original.replace(b"login", b"logon"))
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        stale = self.index.context("alpha", "concept:dry")
        self.assertEqual(stale["node_freshness"]["status"], "stale")
        self.assertEqual(stale["node_freshness"]["files"][0]["reason"], "content_changed")
        self.assertEqual(self.index.context("alpha", "concept:unrelated")["node_freshness"]["status"], "fresh")
        self.assertGreaterEqual(
            self.index.status("alpha")["projects"][0]["semantic_map"]["freshness"]["known_stale_owner_count"], 1
        )
        refused = self.index.update_map("alpha", **changes)
        self.assertEqual(refused["error"], "stale_evidence")
        self.assertEqual(refused["stale_files"], ["src/auth.py"])

    def test_context_evidence_pages_and_mtime_only_changes_remain_fresh(self) -> None:
        paths = []
        for number in range(5):
            relative = f"src/proof-{number}.txt"
            (self.root_a / relative).write_text(f"proof {number}\n", encoding="utf-8")
            paths.append(relative)
        self.assertTrue(self.index.refresh("alpha", "src")["ok"])
        evidence = [
            {"path": relative, "version_token": self.token("alpha", relative)}
            for relative in paths
        ]
        saved = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:proofs", "Concept", "Five proofs", state="confirmed", evidence=evidence,
            )],
        )
        self.assertTrue(saved["ok"], saved)
        page1 = self.index.context("alpha", "concept:proofs", evidence_limit=2)
        self.assertEqual(len(page1["evidence"]), 2)
        self.assertEqual(page1["evidence_page"]["total"], 5)
        self.assertEqual(page1["evidence_page"]["next_offset"], 2)
        page2 = self.index.context("alpha", "concept:proofs", evidence_offset=2, evidence_limit=2)
        page3 = self.index.context("alpha", "concept:proofs", evidence_offset=4, evidence_limit=2)
        self.assertEqual(len(page2["evidence"]), 2)
        self.assertEqual(len(page3["evidence"]), 1)
        self.assertTrue(page3["evidence_page"]["complete"])

        edge_saved = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node("concept:proof-edge", "Concept", "Proof edge"),
                self.semantic_node("concept:proof-peer", "Concept", "Proof peer"),
            ],
            upsert_edges=[{
                "source_id": "concept:proof-edge", "relation": "related_to", "target_id": "concept:proof-peer",
                "evidence": evidence,
            }],
        )
        self.assertTrue(edge_saved["ok"], edge_saved)
        edge_page1 = self.index.context("alpha", "concept:proof-edge", evidence_limit=2)
        self.assertEqual(edge_page1["neighbors"][0]["evidence_page"]["total"], 5)
        self.assertEqual(edge_page1["neighbors"][0]["evidence_page"]["next_offset"], 2)
        edge_page3 = self.index.context(
            "alpha", "concept:proof-edge", evidence_offset=4, evidence_limit=2,
        )
        self.assertEqual(len(edge_page3["neighbors"][0]["evidence"]), 1)
        self.assertTrue(edge_page3["neighbors"][0]["evidence_page"]["complete"])

        target = self.root_a / paths[0]
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        unchanged = self.index.context("alpha", "concept:proofs")
        self.assertEqual(unchanged["node_freshness"]["status"], "fresh")
        with patch.object(
            self.projects["alpha"], "compute_version_token",
            return_value=VersionResult(None, "unavailable", 0),
        ):
            inaccessible = self.index.context("alpha", "concept:proofs")
        self.assertEqual(inaccessible["node_freshness"]["status"], "unknown")
        self.assertFalse(inaccessible["node_freshness"]["complete"])
        status_freshness = self.index.status("alpha")["projects"][0]["semantic_map"]["freshness"]
        self.assertGreaterEqual(status_freshness["last_observed_fresh_owner_count"], 1)
        self.assertGreaterEqual(status_freshness["last_observed_unknown_owner_count"], 1)

    def test_context_freshness_file_count_byte_and_time_budgets(self) -> None:
        evidence = []
        for number in range(21):
            relative = f"src/budget-{number:02d}.txt"
            (self.root_a / relative).write_text(f"budget {number}\n", encoding="utf-8")
            evidence.append(relative)
        self.assertTrue(self.index.refresh("alpha", "src")["ok"])
        evidence_items = [
            {"path": relative, "version_token": self.token("alpha", relative)}
            for relative in evidence
        ]
        saved = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:budgeted-evidence", "Concept", "Budgeted evidence", state="confirmed",
                evidence=evidence_items,
            )],
        )
        self.assertTrue(saved["ok"], saved)
        too_many = self.index.context("alpha", "concept:budgeted-evidence")
        self.assertEqual(too_many["node_freshness"]["status"], "unknown")
        self.assertEqual(too_many["node_freshness"]["reason"], "check_incomplete")
        self.assertEqual(too_many["freshness_check"]["files_checked"], 20)

        with patch("project_preview.semantic_map.MAX_FRESHNESS_BYTES", 4):
            over_bytes = self.index.context("alpha", "concept:budgeted-evidence")
        self.assertEqual(over_bytes["node_freshness"]["status"], "unknown")
        self.assertEqual(over_bytes["node_freshness"]["files"][0]["reason"], "byte_budget")
        self.assertEqual(over_bytes["freshness_check"]["bytes_hashed"], 0)

        with patch("project_preview.semantic_map.MAX_FRESHNESS_SECONDS", 0.0):
            over_time = self.index.context("alpha", "concept:budgeted-evidence")
        self.assertEqual(over_time["node_freshness"]["status"], "unknown")
        self.assertEqual(over_time["node_freshness"]["files"][0]["reason"], "time_budget")

    def test_dry_run_aggregates_independent_errors_and_enforces_reference_budget(self) -> None:
        token = self.token()
        before = self.index.search_map("alpha", "Preflight")["total"]
        aggregated = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node("concept:preflight", "Concept", "Preflight"),
                {"id": "bad id", "type": "Concept", "name": "Invalid", "state": "tentative"},
            ],
            upsert_edges=[{
                "source_id": "concept:preflight", "relation": "maps_to", "target_id": "file:not-present",
            }],
            dry_run=True,
        )
        self.assertEqual(aggregated["error"], "preflight_errors")
        self.assertTrue(aggregated["errors_complete"])
        self.assertEqual({error["error"] for error in aggregated["errors"]}, {"invalid_node_id", "missing_endpoint"})
        self.assertEqual(self.index.search_map("alpha", "Preflight")["total"], before)

        references_32 = [self.semantic_node(
            f"concept:evidence-{number:02d}", "Concept", f"Evidence {number:02d}", state="confirmed",
            evidence=[{"path": "src/auth.py", "version_token": token}],
        ) for number in range(32)]
        accepted = self.index.update_map("alpha", upsert_nodes=references_32, dry_run=True)
        self.assertTrue(accepted["valid"], accepted)
        self.assertEqual(accepted["evidence_references"], 32)
        self.assertEqual(accepted["unique_paths"], 1)
        too_many = self.index.update_map(
            "alpha",
            upsert_nodes=[*references_32, self.semantic_node(
                "concept:evidence-over", "Concept", "Evidence over", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
            dry_run=True,
        )
        self.assertFalse(too_many["valid"])
        self.assertEqual(too_many["errors"][0]["details"]["evidence_references"], 33)
        self.assertEqual(too_many["errors"][0]["details"]["unique_paths"], 1)

        many_invalid = self.index.update_map(
            "alpha",
            upsert_nodes=[
                {"id": f"invalid id {number}", "type": "Concept", "name": f"Invalid {number}", "state": "tentative"}
                for number in range(50)
            ],
            dry_run=True,
        )
        self.assertFalse(many_invalid["errors_complete"])
        self.assertLessEqual(len(many_invalid["errors"]), 50)
        self.assertLessEqual(
            len(json.dumps(many_invalid, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            48_000,
        )

        fresh_token = self.token()
        pending_node = self.semantic_node(
            "concept:dryrun-stale", "Concept", "Dry run must revalidate", state="confirmed",
            evidence=[{"path": "src/auth.py", "version_token": fresh_token}],
        )
        dryrun = self.index.update_map("alpha", upsert_nodes=[pending_node], dry_run=True)
        self.assertTrue(dryrun["valid"], dryrun)
        source = self.root_a / "src" / "auth.py"
        original = source.read_bytes()
        stat = source.stat()
        source.write_bytes(original + b"# changed after dry run\n")
        refused = self.index.update_map("alpha", upsert_nodes=[pending_node])
        self.assertEqual(refused["error"], "stale_evidence")
        self.assertEqual(self.index.search_map("alpha", "Dry run must revalidate")["total"], 0)

    def test_update_map_hash_and_time_budgets_fail_without_writes(self) -> None:
        token = self.token()
        node = self.semantic_node(
            "concept:budget-failure", "Concept", "Budget failure", state="confirmed",
            evidence=[{"path": "src/auth.py", "version_token": token}],
        )
        with patch("project_preview.semantic_map.MAX_EVIDENCE_HASH_BYTES", 0):
            hash_limited = self.index.update_map("alpha", upsert_nodes=[node])
        self.assertEqual(hash_limited["error"], "version_budget")
        with patch("project_preview.semantic_map.MAX_UPDATE_SECONDS", 0.0):
            time_limited = self.index.update_map("alpha", upsert_nodes=[node])
        self.assertEqual(time_limited["error"], "version_budget")
        self.assertEqual(self.index.search_map("alpha", "Budget failure")["total"], 0)

    def test_schema_v4_migration_failure_rolls_back_partial_additions(self) -> None:
        data = self.base / "migration-failure-data"
        failed_store = IndexStore(data)
        failed_store.register_project("legacy", self.root_a)
        connection = sqlite3.connect(failed_store.path)
        try:
            trigger_names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )]
            for trigger_name in trigger_names:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute("DROP TABLE edge_freshness")
            connection.execute("DROP TABLE node_freshness")
            connection.execute("ALTER TABLE map_edges DROP COLUMN roles_json")
            connection.execute("ALTER TABLE projects DROP COLUMN map_revision")
            connection.execute("DROP TABLE map_edges")
            connection.execute("UPDATE schema_meta SET schema_version=4 WHERE singleton=1")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(StoreError):
            IndexStore(data)
        check = sqlite3.connect(failed_store.path)
        try:
            version = check.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            project_columns = {row[1] for row in check.execute("PRAGMA table_info(projects)")}
        finally:
            check.close()
        self.assertEqual(version, 4)
        self.assertNotIn("map_revision", project_columns)

    def test_schema_v4_migrates_roles_and_revision_without_losing_graph_or_version_secret(self) -> None:
        token = self.token()
        file_id = self.file_id("alpha")
        saved = self.index.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:v4", "Concept", "Version four", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
            upsert_edges=[{
                "source_id": "concept:v4", "relation": "maps_to", "target_id": file_id,
                "evidence": [{"path": "src/auth.py", "version_token": token}],
            }],
        )
        self.assertTrue(saved["ok"], saved)
        secret_before = self.store.version_secret()
        connection = sqlite3.connect(self.store.path)
        try:
            trigger_names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )]
            for trigger_name in trigger_names:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute("DROP TABLE edge_freshness")
            connection.execute("DROP TABLE node_freshness")
            connection.execute("ALTER TABLE map_edges DROP COLUMN roles_json")
            connection.execute("ALTER TABLE projects DROP COLUMN map_revision")
            connection.execute("UPDATE schema_meta SET schema_version=4 WHERE singleton=1")
            connection.commit()
        finally:
            connection.close()

        migrated_store = IndexStore(self.base / "service-data")
        migrated_store.register_project("alpha", self.root_a)
        migrated = ProjectIndex({"alpha": self.projects["alpha"]}, migrated_store)
        self.assertEqual(migrated_store.version_secret(), secret_before)
        context = migrated.context("alpha", "concept:v4")
        self.assertTrue(context["ok"], context)
        mapping = next(item for item in context["neighbors"] if item["relation"] == "maps_to")
        self.assertEqual(mapping["roles"], ["unspecified"])
        self.assertEqual(context["node_freshness"]["status"], "fresh")

        accepted = migrated.update_map(
            "alpha",
            upsert_nodes=[self.semantic_node(
                "concept:after-v4", "Concept", "After migration", state="confirmed",
                evidence=[{"path": "src/auth.py", "version_token": token}],
            )],
        )
        self.assertTrue(accepted["ok"], accepted)
        with migrated_store._connection() as connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0], 6)
            self.assertIsNotNone(connection.execute(
                "SELECT map_revision FROM projects WHERE project_id='alpha'"
            ).fetchone())

    def test_export_includes_concept_hierarchy_and_maps_to_roles(self) -> None:
        file_id = self.file_id("alpha")
        saved = self.index.update_map(
            "alpha",
            upsert_nodes=[
                self.semantic_node("concept:system", "Concept", "System"),
                self.semantic_node("concept:mechanism", "Concept", "Mechanism"),
            ],
            upsert_edges=[
                {"source_id": "concept:system", "relation": "contains", "target_id": "concept:mechanism"},
                {"source_id": "concept:mechanism", "relation": "maps_to", "target_id": file_id,
                 "roles": ["implementation", "test"]},
            ],
        )
        self.assertTrue(saved["ok"], saved)
        snapshot = read_graph_snapshot(self.store.path, "alpha")
        markdown = render_graph_markdown(snapshot)
        self.assertIn("| 来源 | 关系 | 目标 | 文件角色 | 管理方式 | 依据文件 |", markdown)
        self.assertIn("implementation, test", markdown)
        self.assertIn("concept:system", markdown)

    def test_stage3_database_v1_migrates_without_losing_project_or_manifest(self) -> None:
        data = self.base / "legacy-data"
        data.mkdir()
        legacy_root = self.base / "legacy-root"
        legacy_root.mkdir()
        (legacy_root / "old.txt").write_text("old source", encoding="utf-8")
        db_path = data / "project-preview.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                "CREATE TABLE schema_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL);"
                "INSERT INTO schema_meta VALUES (1,1);"
                "CREATE TABLE projects(project_id TEXT PRIMARY KEY, root TEXT NOT NULL, root_key TEXT NOT NULL, created_at TEXT NOT NULL);"
                "CREATE TABLE files(project_id TEXT NOT NULL, path TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('file','directory')), "
                "size INTEGER, mtime_ns INTEGER, indexed_at TEXT NOT NULL, PRIMARY KEY(project_id,path), "
                "FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);"
                "CREATE TABLE refresh_runs(refresh_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, scope TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')), started_at TEXT NOT NULL, finished_at TEXT, "
                "file_count INTEGER, directory_count INTEGER, error_code TEXT, error_message TEXT, "
                "FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE);"
                "CREATE INDEX refresh_runs_project_started ON refresh_runs(project_id, started_at DESC, refresh_id DESC);"
            )
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?)",
                ("legacy", str(legacy_root), os.path.normcase(str(legacy_root)), "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO files(project_id,path,type,size,mtime_ns,indexed_at) VALUES (?,?,?,?,?,?)",
                ("legacy", "old.txt", "file", 10, 1, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO refresh_runs(refresh_id,project_id,scope,status,started_at,finished_at,file_count,directory_count) "
                "VALUES ('prior','legacy','','succeeded','2026-01-01T00:00:00+00:00','2026-01-01T00:00:01+00:00',1,0)"
            )
            connection.commit()
        finally:
            connection.close()

        migrated = IndexStore(data)
        migrated.register_project("legacy", legacy_root)
        status = migrated.status("legacy")["projects"][0]
        self.assertEqual(status["discovered_file_count"], 1)
        self.assertEqual(status["last_successful_refresh"]["refresh_id"], "prior")
        self.assertIsNone(status["last_successful_refresh"]["scan_duration_ms"])
        self.assertIsNone(status["last_successful_refresh"]["indexed_bytes"])
        self.assertEqual(status["last_successful_refresh"]["removed_entry_count"], 0)
        manifest = migrated.list_files("legacy")
        self.assertEqual(manifest["entries"][0]["path"], "old.txt")
        self.assertTrue(manifest["entries"][0]["node_id"].startswith("file:"))
        with migrated._connection() as check:
            self.assertEqual(check.execute("SELECT schema_version FROM schema_meta").fetchone()[0], 6)
            self.assertEqual(check.execute("SELECT COUNT(*) FROM map_edges WHERE relation='contains' AND managed=1").fetchone()[0], 1)
        again = IndexStore(data)
        self.assertEqual(again.list_files("legacy")["total"], 1)

    def test_legacy_v2_file_node_evidence_schema_migrates_to_paths(self) -> None:
        data = self.base / "legacy-v2-data"
        root = self.base / "legacy-v2-root"
        root.mkdir()
        (root / "old.txt").write_text("historical evidence", encoding="utf-8")
        store = IndexStore(data)
        store.register_project("legacy", root)
        project = ProjectFiles(root)
        index = ProjectIndex({"legacy": project}, store)
        self.assertTrue(index.refresh("legacy")["ok"])
        file_id = next(
            item["node_id"]
            for item in index.list_files("legacy")["entries"]
            if item["path"] == "old.txt"
        )
        token = index.preview("legacy", "old.txt")["version_token"]
        saved = index.update_map(
            "legacy",
            upsert_nodes=[{
                "id": "concept:legacy-evidence",
                "type": "Concept",
                "name": "Legacy evidence",
                "state": "confirmed",
                "evidence": [{"path": "old.txt", "version_token": token}],
            }],
            upsert_edges=[{
                "source_id": "concept:legacy-evidence",
                "relation": "maps_to",
                "target_id": file_id,
                "evidence": [{"path": "old.txt", "version_token": token}],
            }],
        )
        self.assertTrue(saved["ok"], saved)

        with sqlite3.connect(store.path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TABLE edge_evidence")
            connection.execute("DROP TABLE node_evidence")
            connection.executescript(
                "CREATE TABLE node_evidence ("
                "node_id TEXT NOT NULL, project_id TEXT NOT NULL, file_node_id TEXT NOT NULL, "
                "version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
                "PRIMARY KEY (node_id, file_node_id));"
                "CREATE TABLE edge_evidence ("
                "edge_id TEXT NOT NULL, project_id TEXT NOT NULL, file_node_id TEXT NOT NULL, "
                "version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
                "PRIMARY KEY (edge_id, file_node_id));"
            )
            edge_id = connection.execute(
                "SELECT edge_id FROM map_edges WHERE project_id=? AND relation='maps_to'",
                ("legacy",),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO node_evidence VALUES (?, ?, ?, ?, ?)",
                ("concept:legacy-evidence", "legacy", file_id, token, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO edge_evidence VALUES (?, ?, ?, ?, ?)",
                (edge_id, "legacy", file_id, token, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute("UPDATE schema_meta SET schema_version=2 WHERE singleton=1")
        connection.close()

        migrated = IndexStore(data)
        context = ProjectIndex({"legacy": project}, migrated).context("legacy", "concept:legacy-evidence")
        self.assertTrue(context["ok"], context)
        with migrated._connection() as connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0], 6)
            self.assertEqual(
                connection.execute(
                    "SELECT file_path FROM node_evidence WHERE node_id='concept:legacy-evidence'"
                ).fetchone()[0],
                "old.txt",
            )
            self.assertEqual(
                connection.execute("SELECT file_path FROM edge_evidence WHERE edge_id=?", (edge_id,)).fetchone()[0],
                "old.txt",
            )


if __name__ == "__main__":
    unittest.main()

