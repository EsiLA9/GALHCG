from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mcp import Client, StdioServerParameters


def _tool_data(result: object) -> dict[str, object]:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text":
            value = json.loads(item.text)
            if isinstance(value, dict):
                return value
    raise AssertionError("MCP 工具没有返回可解析的数据。")


class StdioMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_stdio_handshake_browse_preview_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "项目" / "示例"
            source = root / "src" / "main.py"
            source.parent.mkdir(parents=True)
            source.write_text("第一行\n第二行\n", encoding="utf-8")
            expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            params = StdioServerParameters(
                command=str(Path(__import__("sys").executable).resolve()),
                args=[
                    "-m", "project_preview", "--root", str(root.resolve()),
                    "--data-dir", str((Path(temporary) / "service-data").resolve()),
                ],
                cwd=temporary,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )

            async with Client(params, mode="legacy", raise_exceptions=True) as client:
                listed_tools = await client.list_tools()
                self.assertEqual(
                    {tool.name for tool in listed_tools.tools},
                    {"browse", "preview", "search", "refresh", "refresh_history", "status", "list_files", "update_map", "context", "traverse", "resolve_paths"},
                )

                refresh_result = await client.call_tool("refresh", {})
                refresh = _tool_data(refresh_result)
                self.assertTrue(refresh["ok"], refresh)
                self.assertEqual(refresh["project_id"], "default")
                manifest_result = await client.call_tool("list_files", {"limit": 10})
                manifest = _tool_data(manifest_result)
                self.assertTrue(manifest["ok"], manifest)
                manifest_file = next(entry for entry in manifest["entries"] if entry["path"] == "src/main.py")
                self.assertEqual(manifest_file["type"], "file")
                self.assertTrue(manifest_file["node_id"].startswith("file:"))
                resolved_result = await client.call_tool(
                    "resolve_paths", {"project_id": "default", "paths": ["src/main.py"]}
                )
                resolved = _tool_data(resolved_result)
                self.assertEqual(resolved["results"][0]["node_id"], manifest_file["node_id"])
                status_result = await client.call_tool("status", {"project_id": "default"})
                status = _tool_data(status_result)
                self.assertEqual(status["projects"][0]["discovered_file_count"], 1)
                history_result = await client.call_tool(
                    "refresh_history", {"project_id": "default", "limit": 10}
                )
                history = _tool_data(history_result)
                self.assertEqual(history["total"], 1)
                self.assertEqual(history["runs"][0]["status"], "succeeded")
                self.assertEqual(history["runs"][0]["indexed_bytes"], source.stat().st_size)
                self.assertIsInstance(history["runs"][0]["elapsed_ms"], int)

                browse_result = await client.call_tool("browse", {"directory": "src", "limit": 10})
                self.assertFalse(browse_result.is_error)
                browse = _tool_data(browse_result)
                self.assertTrue(browse["ok"])
                self.assertEqual(browse["entries"], [{"path": "src/main.py", "type": "file"}])

                preview_result = await client.call_tool(
                    "preview", {"path": "src/main.py", "start_line": 2, "line_count": 1}
                )
                self.assertFalse(preview_result.is_error)
                preview = _tool_data(preview_result)
                self.assertEqual(preview["lines"], [{"line_number": 2, "content": "第二行"}])
                self.assertEqual(preview["version_token_status"], "available")
                self.assertRegex(preview["version_token"], r"^v1\.[A-Za-z0-9_-]{43}$")

                update_payload = {
                    "project_id": "default",
                    "upsert_nodes": [
                        {
                            "id": "concept:stdio-entry",
                            "type": "Concept",
                            "name": "MCP entry",
                            "summary": "Entry point read through stdio.",
                            "aliases": ["protocol entry"],
                            "state": "confirmed",
                            "evidence": [
                                {"path": "src/main.py", "version_token": preview["version_token"]}
                            ],
                        }
                    ],
                    "upsert_edges": [
                        {
                            "source_id": "concept:stdio-entry",
                            "relation": "maps_to",
                            "target_id": manifest_file["node_id"],
                            "roles": ["implementation"],
                            "evidence": [
                                {"path": "src/main.py", "version_token": preview["version_token"]}
                            ],
                        }
                    ],
                }
                dry_run_result = await client.call_tool(
                    "update_map", {**update_payload, "dry_run": True}
                )
                dry_run = _tool_data(dry_run_result)
                self.assertTrue(dry_run["valid"], dry_run)
                self.assertEqual(dry_run["evidence_references"], 2)
                self.assertEqual(_tool_data(await client.call_tool(
                    "search", {"mode": "map", "query": "MCP entry", "project_id": "default"}
                ))["total"], 0)

                update_result = await client.call_tool(
                    "update_map",
                    update_payload,
                )
                update = _tool_data(update_result)
                self.assertTrue(update["ok"], update)
                context_result = await client.call_tool(
                    "context", {"project_id": "default", "node_id": "concept:stdio-entry"}
                )
                context = _tool_data(context_result)
                self.assertTrue(context["ok"], context)
                self.assertEqual(context["files"][0]["path"], "src/main.py")
                self.assertEqual(context["neighbors"][0]["roles"], ["implementation"])
                traverse_result = await client.call_tool(
                    "traverse", {
                        "project_id": "default", "start_node_id": "concept:stdio-entry",
                        "relations": ["maps_to"], "direction": "outgoing", "max_depth": 1,
                    },
                )
                traversal = _tool_data(traverse_result)
                self.assertEqual(traversal["total_nodes"], 2)
                self.assertEqual(traversal["edges"][0]["target_id"], manifest_file["node_id"])
                map_search_result = await client.call_tool(
                    "search", {"mode": "map", "query": "protocol entry", "node_types": ["Concept"], "project_id": "default"}
                )
                map_search = _tool_data(map_search_result)
                self.assertEqual(map_search["results"][0]["id"], "concept:stdio-entry")

                search_result = await client.call_tool(
                    "search", {"mode": "source", "query": "第二行", "directory": "src"}
                )
                self.assertFalse(search_result.is_error)
                search = _tool_data(search_result)
                self.assertEqual(search["outcome"], "complete")
                self.assertEqual(search["results"][0]["path"], "src/main.py")
                self.assertEqual(search["results"][0]["line_number"], 2)

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), expected_hash)
            self.assertEqual({path.name for path in root.iterdir()}, {"src"})

    async def test_multi_project_selection_isolation_and_restart_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root_a = base / "项目 A"
            root_b = base / "项目 B"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "same.txt").write_text("alpha only", encoding="utf-8")
            (root_b / "same.txt").write_text("beta only", encoding="utf-8")
            data_dir = base / "index-data"
            args = [
                "-m", "project_preview",
                "--project", f"alpha={root_a.resolve()}",
                "--project", f"beta={root_b.resolve()}",
                "--data-dir", str(data_dir.resolve()),
            ]
            params = StdioServerParameters(
                command=str(Path(__import__("sys").executable).resolve()), args=args, cwd=base
            )

            async with Client(params, mode="legacy", raise_exceptions=True) as client:
                missing_id = _tool_data(await client.call_tool("browse", {}))
                self.assertEqual(missing_id["error"], "project_id_required")
                for project_id in ("alpha", "beta"):
                    refreshed = _tool_data(await client.call_tool("refresh", {"project_id": project_id}))
                    self.assertTrue(refreshed["ok"], refreshed)
                alpha = _tool_data(await client.call_tool("preview", {"project_id": "alpha", "path": "same.txt"}))
                beta = _tool_data(await client.call_tool("preview", {"project_id": "beta", "path": "same.txt"}))
                self.assertEqual(alpha["lines"][0]["content"], "alpha only")
                self.assertEqual(beta["lines"][0]["content"], "beta only")
                self.assertEqual(_tool_data(await client.call_tool("list_files", {"project_id": "alpha"}))["total"], 1)

            async with Client(params, mode="legacy", raise_exceptions=True) as client:
                status = _tool_data(await client.call_tool("status", {}))
                self.assertEqual(status["configured_project_count"], 2)
                self.assertEqual(
                    {item["project_id"]: item["discovered_file_count"] for item in status["projects"]},
                    {"alpha": 1, "beta": 1},
                )


if __name__ == "__main__":
    unittest.main()
