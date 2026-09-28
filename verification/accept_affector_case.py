from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project_preview.filesystem import ProjectFiles  # noqa: E402
from project_preview.index import ProjectIndex  # noqa: E402
from project_preview.store import IndexStore  # noqa: E402


OUTPUT_LIMIT_BYTES = 48_000
PREVIEW_LINES = 40
SOURCE_FILES = [
    "src/engine/index.ts",
    "src/engine/expression/condition-deps.ts",
    "src/engine/effect/affector-engine.ts",
    "src/engine/system/tick-system.ts",
    "tests/engine/event-driven-reactor.test.ts",
    "tests/engine/affector-engine.test.ts",
    "tests/engine/affector-reconcile.test.ts",
    "docs/docs-829/04-mechanisms/engine/effect-trigger.md",
]

CASES = [
    {
        "id": "q1_event_and_polling",
        "prompt": "Affector 是否每 Tick 全量重估？",
        "concept_query": "affector rules",
        "source_query": "registerConditionDeps",
        "path_query": "affector-engine.ts",
        "fallback_path_query": "condition-deps.ts",
    },
    {
        "id": "q2_stat_fallback",
        "prompt": "为什么 stat 条件仍需 Tick？",
        "concept_query": "stat polling fallback",
        "source_query": "STAT_DEP_EVENTS",
        "path_query": "condition-deps.ts",
        "fallback_path_query": "event-driven-reactor.test.ts",
    },
    {
        "id": "q3_tick_boundaries",
        "prompt": "停止条件重估是否等于停止资源产出？",
        "concept_query": "explicit per Tick effects",
        "source_query": "perTickEffects",
        "path_query": "tick-system.ts",
        "fallback_path_query": "effect-trigger.md",
    },
    {
        "id": "q4_evidence_freshness",
        "prompt": "这个结论依据还新鲜吗？",
        "concept_query": "stat polling fallback",
        "source_query": "ConditionDepIndex",
        "path_query": "condition-deps.ts",
        "fallback_path_query": "condition-deps.ts",
        "mutate_path": "src/engine/expression/condition-deps.ts",
    },
    {
        "id": "q5_unmapped_topic",
        "prompt": "某个尚未建图的区域怎么办？",
        "concept_query": "unmapped district policy",
        "source_query": "spotId",
        "path_query": "spot",
        "fallback_path_query": "tick-system.ts",
    },
]


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _call(route: dict[str, Any], name: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    route["calls"].append({"tool": name, "params": params, "response": result})
    route["output_bytes"] += _json_bytes(result)
    if name == "preview":
        route["preview_calls"] += 1
        route["preview_lines"] += len(result.get("lines", []))
        route["preview_paths"].append(params["path"])
    return result


def _new_route(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id, "calls": [], "output_bytes": 0,
        "preview_calls": 0, "preview_lines": 0, "preview_paths": [],
        "freshness_verdict": None, "_started": time.perf_counter(),
    }


def _finish_route(route: dict[str, Any]) -> dict[str, Any]:
    started = route.pop("_started", None)
    if started is not None:
        route["elapsed_ms_auxiliary_only"] = max(0, int((time.perf_counter() - started) * 1000))
    return route


def _fallback_route(index: ProjectIndex, project_id: str, case: dict[str, Any]) -> dict[str, Any]:
    route = _new_route(project_id)
    project = index.projects[project_id]
    path_params = {
        "mode": "path", "query": case["path_query"], "directory": "",
        "limit": 10, "case_sensitive": True,
    }
    path_result = _call(route, "search", {**path_params, "project_id": project_id}, project.search(**path_params))
    source_params = {
        "mode": "source", "query": case["source_query"], "directory": "",
        "limit": 10, "case_sensitive": True,
    }
    source_result = _call(route, "search", {**source_params, "project_id": project_id}, project.search(**source_params))
    hits = source_result.get("results", [])[:3]
    if not hits:
        fallback_params = {
            "mode": "path", "query": case["fallback_path_query"], "directory": "",
            "limit": 10, "case_sensitive": True,
        }
        fallback = _call(route, "search", {**fallback_params, "project_id": project_id}, project.search(**fallback_params))
        hits = fallback.get("results", [])[:3]
    paths = list(dict.fromkeys(item["path"] for item in hits if item.get("path")))
    if not paths:
        paths = [item["path"] for item in path_result.get("results", [])[:2] if item.get("path")]
    for path in paths[:3]:
        params = {"path": path, "start_line": 1, "line_count": PREVIEW_LINES}
        _call(route, "preview", params, index.preview(project_id, **params))
    if case["id"] == "q4_evidence_freshness":
        route["freshness_verdict"] = "unsupported_by_path/source/preview route"
    route["source_search_hit_count"] = len(source_result.get("results", []))
    return _finish_route(route)


def _map_route(index: ProjectIndex, project_id: str, case: dict[str, Any]) -> dict[str, Any]:
    route = _new_route(project_id)
    query = case["concept_query"]
    search_params = {
        "mode": "map", "query": query, "case_sensitive": False,
        "node_types": ["Concept"], "limit": 10, "offset": 0, "project_id": project_id,
    }
    found = _call(
        route, "search_map", search_params,
        index.search_map(project_id, query, case_sensitive=False, node_types=["Concept"], limit=10, offset=0),
    )
    concept_ids = [item["id"] for item in found.get("results", [])[:3]]
    route["concept_ids"] = concept_ids
    route["semantic_hit_count"] = found.get("total", 0)
    if not concept_ids:
        route["fallback_used"] = True
        fallback = _fallback_route(index, project_id, case)
        route["calls"].extend(fallback["calls"])
        route["output_bytes"] += fallback["output_bytes"]
        route["preview_calls"] += fallback["preview_calls"]
        route["preview_lines"] += fallback["preview_lines"]
        route["preview_paths"].extend(fallback["preview_paths"])
        route["freshness_verdict"] = fallback["freshness_verdict"]
        route["source_search_hit_count"] = fallback.get("source_search_hit_count", 0)
        return _finish_route(route)

    route["fallback_used"] = False
    for node_id in concept_ids:
        context_params = {"node_id": node_id, "neighbor_limit": 20, "evidence_limit": 20, "project_id": project_id}
        context = _call(
            route, "context", context_params,
            index.context(project_id, node_id, neighbor_limit=20, evidence_limit=20),
        )
        traverse_params = {
            "start_node_id": node_id, "relations": ["contains", "maps_to"],
            "direction": "outgoing", "max_depth": 4, "node_limit": 50, "edge_limit": 100,
            "project_id": project_id,
        }
        traversal = _call(
            route, "traverse", traverse_params,
            index.traverse(
                project_id, node_id, target_node_id=None, relations=traverse_params["relations"],
                direction="outgoing", max_depth=4, node_limit=50, edge_limit=100,
            ),
        )
        if case["id"] == "q4_evidence_freshness":
            statuses = [context.get("node_freshness", {}).get("status")]
            statuses.extend(edge.get("freshness", {}).get("status") for edge in context.get("neighbors", []))
            route["freshness_verdict"] = "stale" if "stale" in statuses else (statuses[0] if statuses else "unknown")
            stale_paths = set()
            for item in context.get("node_freshness", {}).get("files", []):
                if item.get("status") == "stale":
                    stale_paths.add(item["path"])
            for edge in context.get("neighbors", []):
                for item in edge.get("freshness", {}).get("files", []):
                    if item.get("status") == "stale":
                        stale_paths.add(item["path"])
        else:
            stale_paths = set()
        mapped_paths = [
            node.get("path") for node in traversal.get("nodes", [])
            if node.get("type") == "File" and node.get("path")
        ]
        if case["id"] == "q4_evidence_freshness":
            mapped_paths = [path for path in mapped_paths if path in stale_paths]
        elif not mapped_paths:
            mapped_paths = [item["path"] for item in context.get("files", []) if item.get("type") == "File"]
        for path in list(dict.fromkeys(mapped_paths))[:4]:
            params = {"path": path, "start_line": 1, "line_count": PREVIEW_LINES}
            _call(route, "preview", params, index.preview(project_id, **params))
    return _finish_route(route)


def _build_graph(
    index: ProjectIndex, project_id: str, tokens: dict[str, str], file_ids: dict[str, str]
) -> dict[str, Any]:
    node_specs = [
        ("module:engine", "Module", "Engine", "Engine implementation and contracts.", ["engine"], "src/engine/index.ts"),
        ("concept:engine-continuous-rules", "Concept", "Engine continuous rules", "Condition-driven rules maintained by Affector instances.", ["affector rules"], "docs/docs-829/04-mechanisms/engine/effect-trigger.md"),
        ("concept:affector-freshness", "Concept", "Affector lifecycle and condition freshness", "Active entries are refreshed by matching events; stat and unknown dependencies keep polling fallback.", ["Affector freshness", "lifecycle"], "src/engine/effect/affector-engine.ts"),
        ("concept:condition-event-invalidation", "Concept", "Condition event invalidation", "Condition leaves register targeted event dependencies for recheck.", ["event-driven invalidation"], "src/engine/expression/condition-deps.ts"),
        ("concept:condition-polling-fallback", "Concept", "Condition polling fallback", "Stat or unknown dependencies remain in the bounded per-Tick polling set.", ["stat polling fallback"], "src/engine/expression/condition-deps.ts"),
        ("concept:active-entry-transition", "Concept", "Active Entry transition", "The Latent to Active transition applies entry.effects once.", ["activation edge"], "src/engine/effect/affector-engine.ts"),
        ("concept:explicit-per-tick-effects", "Concept", "Explicit per-Tick effects", "Only declared perTickEffects execute on each active Affector tick.", ["explicit per Tick effects", "perTickEffects"], "src/engine/system/tick-system.ts"),
        ("concept:derived-contributions", "Concept", "Derived contributions", "Active flows and zone modifiers are synchronized as derived contributions.", ["flows", "zone modifiers"], "docs/docs-829/04-mechanisms/engine/effect-trigger.md"),
    ]
    nodes = []
    for node_id, node_type, name, summary, aliases, path in node_specs:
        nodes.append({
            "id": node_id, "type": node_type, "name": name, "summary": summary,
            "aliases": aliases, "state": "confirmed",
            "evidence": [{"path": path, "version_token": tokens[path]}],
        })

    edge_specs = [
        ("module:engine", "contains", "concept:engine-continuous-rules", "src/engine/index.ts"),
        ("concept:engine-continuous-rules", "contains", "concept:affector-freshness", "docs/docs-829/04-mechanisms/engine/effect-trigger.md"),
        ("concept:affector-freshness", "contains", "concept:condition-event-invalidation", "src/engine/effect/affector-engine.ts"),
        ("concept:affector-freshness", "contains", "concept:condition-polling-fallback", "src/engine/effect/affector-engine.ts"),
        ("concept:affector-freshness", "contains", "concept:active-entry-transition", "src/engine/effect/affector-engine.ts"),
        ("concept:affector-freshness", "contains", "concept:explicit-per-tick-effects", "src/engine/effect/affector-engine.ts"),
        ("concept:affector-freshness", "contains", "concept:derived-contributions", "src/engine/effect/affector-engine.ts"),
    ]
    edge_specs = [
        {"source_id": source, "relation": relation, "target_id": target,
         "evidence": [{"path": path, "version_token": tokens[path]}]}
        for source, relation, target, path in edge_specs
    ]
    map_specs = [
        ("concept:condition-event-invalidation", [
            ("src/engine/effect/affector-engine.ts", "implementation"),
            ("src/engine/expression/condition-deps.ts", "implementation"),
            ("tests/engine/event-driven-reactor.test.ts", "test"),
        ]),
        ("concept:condition-polling-fallback", [
            ("src/engine/expression/condition-deps.ts", "implementation"),
            ("src/engine/effect/affector-engine.ts", "implementation"),
            ("tests/engine/event-driven-reactor.test.ts", "test"),
        ]),
        ("concept:active-entry-transition", [
            ("src/engine/effect/affector-engine.ts", "implementation"),
            ("tests/engine/affector-engine.test.ts", "test"),
            ("tests/engine/affector-reconcile.test.ts", "test"),
        ]),
        ("concept:explicit-per-tick-effects", [
            ("src/engine/effect/affector-engine.ts", "implementation"),
            ("src/engine/system/tick-system.ts", "implementation"),
            ("docs/docs-829/04-mechanisms/engine/effect-trigger.md", "documentation"),
            ("tests/engine/affector-reconcile.test.ts", "test"),
        ]),
        ("concept:derived-contributions", [
            ("src/engine/effect/affector-engine.ts", "implementation"),
            ("src/engine/system/tick-system.ts", "implementation"),
            ("docs/docs-829/04-mechanisms/engine/effect-trigger.md", "documentation"),
        ]),
    ]
    for node_id, mappings in map_specs:
        for path, role in mappings:
            edge_specs.append({
                "source_id": node_id, "relation": "maps_to", "target_id": file_ids[path],
                "roles": [role], "evidence": [{"path": path, "version_token": tokens[path]}],
            })
    if sum(len(item["evidence"]) for item in nodes) + sum(len(item["evidence"]) for item in edge_specs) > 32:
        raise RuntimeError("Sample graph exceeded the evidence-reference budget.")
    return index.update_map(project_id, upsert_nodes=nodes, upsert_edges=edge_specs)


def run(acprogram_root: Path, output: Path) -> dict[str, Any]:
    acprogram_root = acprogram_root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="galhcg-affector-case-") as temporary:
        temp_root = Path(temporary)
        roots = {"acprogram": temp_root / "acprogram", "other": temp_root / "other"}
        for root in roots.values():
            for relative in SOURCE_FILES:
                target = root / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(acprogram_root / Path(relative), target)

        store = IndexStore(temp_root / "service-data")
        projects = {project_id: ProjectFiles(root) for project_id, root in roots.items()}
        for project_id, project in projects.items():
            store.register_project(project_id, project.root)
        index = ProjectIndex(projects, store)
        refresh_results = {project_id: index.refresh(project_id) for project_id in projects}
        if not all(item.get("ok") for item in refresh_results.values()):
            raise RuntimeError(f"Fixture refresh failed: {refresh_results}")

        source_hashes = {
            relative: hashlib.sha256((roots["acprogram"] / Path(relative)).read_bytes()).hexdigest()
            for relative in SOURCE_FILES
        }
        tokens: dict[str, str] = {}
        build_previews = []
        started = time.perf_counter()
        for relative in SOURCE_FILES:
            preview = index.preview("acprogram", relative, line_count=1)
            if not preview.get("version_token"):
                raise RuntimeError(f"No preview token for {relative}: {preview}")
            tokens[relative] = preview["version_token"]
            build_previews.append({"path": relative, "response": preview})
        mapped_paths = sorted({
            path for _, mappings in [
                ("condition-event-invalidation", [("src/engine/effect/affector-engine.ts", "implementation"),
                    ("src/engine/expression/condition-deps.ts", "implementation"),
                    ("tests/engine/event-driven-reactor.test.ts", "test")]),
                ("condition-polling-fallback", [("src/engine/expression/condition-deps.ts", "implementation"),
                    ("src/engine/effect/affector-engine.ts", "implementation"),
                    ("tests/engine/event-driven-reactor.test.ts", "test")]),
                ("active-entry-transition", [("src/engine/effect/affector-engine.ts", "implementation"),
                    ("tests/engine/affector-engine.test.ts", "test"),
                    ("tests/engine/affector-reconcile.test.ts", "test")]),
                ("explicit-per-tick-effects", [("src/engine/effect/affector-engine.ts", "implementation"),
                    ("src/engine/system/tick-system.ts", "implementation"),
                    ("docs/docs-829/04-mechanisms/engine/effect-trigger.md", "documentation"),
                    ("tests/engine/affector-reconcile.test.ts", "test")]),
                ("derived-contributions", [("src/engine/effect/affector-engine.ts", "implementation"),
                    ("src/engine/system/tick-system.ts", "implementation"),
                    ("docs/docs-829/04-mechanisms/engine/effect-trigger.md", "documentation")]),
            ] for path, _role in mappings
        })
        path_resolution = index.resolve_paths("acprogram", mapped_paths)
        if not path_resolution.get("ok") or path_resolution["error_count"]:
            raise RuntimeError(f"Could not resolve sample graph paths: {path_resolution}")
        file_ids = {item["path"]: item["node_id"] for item in path_resolution["results"]}
        graph_result = _build_graph(index, "acprogram", tokens, file_ids)
        graph_elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))

        beta_preview = index.preview("other", SOURCE_FILES[1], line_count=1)
        beta_token = beta_preview["version_token"]
        beta_resolution = index.resolve_paths("other", [SOURCE_FILES[1]])
        beta_file = beta_resolution["results"][0]["node_id"]
        beta_graph = index.update_map("other", upsert_nodes=[{
            "id": "concept:affector-freshness", "type": "Concept", "name": "Private beta concept",
            "summary": "Only present in the other project.", "state": "confirmed",
            "evidence": [{"path": SOURCE_FILES[1], "version_token": beta_token}],
        }], upsert_edges=[{
            "source_id": "concept:affector-freshness", "relation": "maps_to", "target_id": beta_file,
            "roles": ["implementation"], "evidence": [{"path": SOURCE_FILES[1], "version_token": beta_token}],
        }])
        if not graph_result.get("ok") or not beta_graph.get("ok"):
            raise RuntimeError(f"Graph setup failed: alpha={graph_result}, beta={beta_graph}")

        repetitions: dict[str, Any] = {}
        freshness_path = roots["acprogram"] / CASES[3]["mutate_path"]
        original_bytes = freshness_path.read_bytes()
        original_stat = freshness_path.stat()
        for case in CASES:
            case_runs = {"prompt": case["prompt"], "A_fallback": [], "B_semantic": []}
            if case["id"] == "q4_evidence_freshness":
                case_runs["B_fresh_map_reuse"] = []
            for repetition in range(1, 4):
                case_runs["A_fallback"].append({"repetition": repetition, **_fallback_route(index, "acprogram", case)})
                if case["id"] == "q4_evidence_freshness":
                    case_runs["B_fresh_map_reuse"].append({
                        "repetition": repetition, **_map_route(index, "acprogram", case),
                    })
                    freshness_path.write_bytes(original_bytes + b"\n// temporary acceptance fixture mutation\n")
                try:
                    case_runs["B_semantic"].append({"repetition": repetition, **_map_route(index, "acprogram", case)})
                finally:
                    if case["id"] == "q4_evidence_freshness":
                        freshness_path.write_bytes(original_bytes)
                        os.utime(freshness_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            repetitions[case["id"]] = case_runs

        isolation_runs = []
        isolation_case = {
            "path_query": "condition-deps.ts", "source_query": "ConditionDepIndex",
        }
        for repetition in range(1, 4):
            fallback_routes = []
            for project_id in ("acprogram", "other"):
                route = _new_route(project_id)
                path_params = {
                    "mode": "path", "query": isolation_case["path_query"], "directory": "",
                    "limit": 10, "case_sensitive": True,
                }
                _call(route, "search", {**path_params, "project_id": project_id}, projects[project_id].search(**path_params))
                source_params = {
                    "mode": "source", "query": isolation_case["source_query"], "directory": "",
                    "limit": 10, "case_sensitive": True,
                }
                source = _call(route, "search", {**source_params, "project_id": project_id}, projects[project_id].search(**source_params))
                for hit in source.get("results", [])[:1]:
                    preview_params = {"path": hit["path"], "start_line": 1, "line_count": PREVIEW_LINES}
                    _call(route, "preview", {**preview_params, "project_id": project_id}, index.preview(project_id, **preview_params))
                resolved_params = {"paths": [SOURCE_FILES[1]], "project_id": project_id}
                _call(route, "resolve_paths", resolved_params, index.resolve_paths(project_id, resolved_params["paths"]))
                fallback_routes.append(_finish_route(route))

            semantic_route = _new_route("multi-project")
            semantic_route["project_ids"] = ["acprogram", "other"]
            for project_id, query in (
                ("acprogram", "Affector freshness"),
                ("other", "Private beta concept"),
            ):
                search_params = {
                    "mode": "map", "query": query, "case_sensitive": False, "node_types": ["Concept"],
                    "limit": 10, "project_id": project_id,
                }
                _call(
                    semantic_route, "search_map", search_params,
                    index.search_map(project_id, query, case_sensitive=False, node_types=["Concept"], limit=10),
                )
            for project_id in ("acprogram", "other"):
                context_params = {"node_id": "concept:affector-freshness", "project_id": project_id}
                _call(
                    semantic_route, "context", context_params,
                    index.context(project_id, context_params["node_id"]),
                )
                resolve_params = {"paths": [SOURCE_FILES[1]], "project_id": project_id}
                _call(
                    semantic_route, "resolve_paths", resolve_params,
                    index.resolve_paths(project_id, resolve_params["paths"]),
                )
            _finish_route(semantic_route)
            isolation_runs.append({
                "repetition": repetition,
                "A_fallback": fallback_routes,
                "B_semantic": semantic_route,
            })
        repetitions["q6_project_isolation"] = {
            "prompt": "相同路径和节点 ID 是否会串到另一项目？",
            "A_fallback": [run["A_fallback"] for run in isolation_runs],
            "B_semantic": [run["B_semantic"] for run in isolation_runs],
        }

        isolation = {
            "same_node_id": "concept:affector-freshness",
            "alpha_context": index.context("acprogram", "concept:affector-freshness"),
            "beta_context": index.context("other", "concept:affector-freshness"),
            "alpha_private_beta_search": index.search_map("acprogram", "Private beta concept", case_sensitive=False, node_types=["Concept"]),
            "beta_private_beta_search": index.search_map("other", "Private beta concept", case_sensitive=False, node_types=["Concept"]),
            "alpha_resolved_file_id": index.resolve_paths("acprogram", [SOURCE_FILES[1]])["results"][0]["node_id"],
            "beta_resolved_file_id": index.resolve_paths("other", [SOURCE_FILES[1]])["results"][0]["node_id"],
        }
        after_hash = hashlib.sha256(freshness_path.read_bytes()).hexdigest()
        return {
            "title": "Affector condition freshness: route comparison",
            "date": "2026-09-28",
            "fixture": {
                "source_root": str(acprogram_root), "project_ids": ["acprogram", "other"],
                "source_files": source_hashes, "restored_condition_file_sha256": after_hash,
                "source_snapshot_restored": after_hash == source_hashes["src/engine/expression/condition-deps.ts"],
                "output_limit_bytes_per_tool": OUTPUT_LIMIT_BYTES, "preview_line_limit": PREVIEW_LINES,
                "database_scope": "temporary directory; discarded after run; no existing project index modified",
            },
            "graph_build": {
                "refresh_responses": refresh_results,
                "preview_responses": build_previews,
                "path_resolution_response": path_resolution,
                "update_map_response": graph_result,
                "beta_isolation_update_response": beta_graph,
                "beta_setup_preview_response": beta_preview,
                "beta_setup_path_resolution_response": beta_resolution,
                "elapsed_ms_auxiliary_only": graph_elapsed_ms,
                "node_count": 8, "edge_count": 23,
                "evidence_reference_count": 31, "unique_evidence_paths": len(SOURCE_FILES),
                "note": "Graph construction and evidence-reading cost are reported separately from repeated reuse.",
            },
            "cases": repetitions,
            "project_isolation": isolation,
            "interpretation": {
                "correctness_basis": "Manually checked against current source, tests, and docs/docs-829/04-mechanisms/engine/effect-trigger.md.",
                "timing": "Single warm local run; latency is auxiliary and does not establish long-term speedup.",
                "comparison": "Each tool response has the same 48,000-byte hard output ceiling; repetitions use the same copied source snapshot except the explicit stale-evidence fixture test.",
                "limitations": [
                    "The fallback route cannot label persisted evidence fresh or stale.",
                    "The map was manually built, so its cold construction cost is shown separately.",
                    "The experiment measures query routing and reading volume, not Affector runtime performance.",
                ],
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an isolated, read-only-source Affector semantic-map acceptance case.")
    parser.add_argument("--acprogram-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO / "verification" / "affector_case_results.json")
    args = parser.parse_args()
    result = run(args.acprogram_root, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "source_snapshot_restored": result["fixture"]["source_snapshot_restored"],
        "graph_build": result["graph_build"]["update_map_response"],
            "case_count": len(result["cases"]),
        "repetitions_per_route": 3,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
