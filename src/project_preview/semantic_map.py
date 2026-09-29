from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import stat
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from project_preview.filesystem import ProjectFiles, ToolFailure
from project_preview.store import IndexStore, StoreError, utc_now


MAX_NODE_ID_CHARS = 128
MAX_NODE_NAME_CHARS = 120
MAX_NODE_SUMMARY_CHARS = 500
MAX_ALIASES = 20
MAX_ALIAS_CHARS = 120
MAX_UPSERT_NODES = 50
MAX_DELETE_NODES = 50
MAX_UPSERT_EDGES = 100
MAX_DELETE_EDGES = 100
MAX_EVIDENCE_REFERENCES = 32
MAX_EVIDENCE_HASH_BYTES = 256 * 1024 * 1024
MAX_UPDATE_SECONDS = 15.0
MAX_CONTEXT_NEIGHBORS = 20
MAX_CONTEXT_DEPTH = 10
MAX_CONTEXT_EVIDENCE = 20
MAX_FRESHNESS_FILES = 20
MAX_FRESHNESS_BYTES = 256 * 1024 * 1024
MAX_FRESHNESS_SECONDS = 15.0
MAX_CHANGE_PAGE_FILES = 20
MAX_CHANGE_OWNER_PAGE = 5
MAX_CHANGE_BYTES = 256 * 1024 * 1024
MAX_CHANGE_SECONDS = 15.0
MAX_MAP_SEARCH_LIMIT = 100
MAX_MAP_SEARCH_OFFSET = (1 << 63) - 1
MAX_MAP_OUTPUT_BYTES = 48_000
MAX_TRAVERSE_DEPTH = 8
MAX_TRAVERSE_NODES = 1_000
MAX_TRAVERSE_EDGES = 2_000
MAX_TRAVERSE_SECONDS = 2.0
MAX_TRAVERSE_PAGE_NODES = 50
MAX_TRAVERSE_PAGE_EDGES = 100
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
VERSION_TOKEN_RE = re.compile(r"^v1\.[A-Za-z0-9_-]{43}$")

NODE_TYPES = {"Project", "Module", "Concept", "File"}
SEMANTIC_TYPES = {"Module", "Concept"}
RELATION_TYPES = {"contains", "maps_to", "depends_on", "related_to"}
TRAVERSE_RELATIONS = RELATION_TYPES
ALLOWED_ENDPOINTS = {
    "contains": {
        ("Project", "Module"), ("Project", "File"),
        ("Module", "Module"), ("Module", "Concept"), ("Module", "File"),
        ("Concept", "Concept"),
    },
    "maps_to": {("Concept", "File")},
    "depends_on": {("Module", "Module"), ("Concept", "Concept")},
    "related_to": {("Concept", "Concept")},
}


class MapFailure(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class DryRunRollback(Exception):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("预检事务已回滚。")
        self.report = report


class SemanticMap:
    def __init__(self, store: IndexStore, projects: dict[str, ProjectFiles]) -> None:
        self.store = store
        self.projects = projects

    @staticmethod
    def _array(value: Any, name: str, maximum: int) -> list[Any]:
        if value is None:
            return []
        if type(value) is not list:
            raise MapFailure("invalid_input", f"{name} 必须是数组。")
        if len(value) > maximum:
            raise MapFailure("invalid_input", f"{name} 最多接受 {maximum} 项。")
        return value

    @staticmethod
    def _node_id(value: Any, expected_type: str | None = None, *, allow_managed: bool = False) -> str:
        if not isinstance(value, str) or len(value) > MAX_NODE_ID_CHARS or not NODE_ID_RE.fullmatch(value):
            raise MapFailure("invalid_node_id", f"节点 ID 必须是 1–{MAX_NODE_ID_CHARS} 个安全 ASCII 字符。")
        if value.startswith(("project:", "file:")) and not allow_managed:
            raise MapFailure("managed_node", "Project 和 File 节点由服务维护，不能通过 update_map 写入或删除。")
        if allow_managed and value.startswith(("project:", "file:")):
            if value.endswith(":"):
                raise MapFailure("invalid_node_id", "系统节点 ID 缺少稳定后缀。")
            return value
        if not (value.startswith("module:") or value.startswith("concept:")):
            raise MapFailure("invalid_node_id", "语义节点 ID 必须以 module: 或 concept: 开头。")
        if value.endswith(":"):
            raise MapFailure("invalid_node_id", "语义节点 ID 的前缀后必须包含稳定标识。")
        if expected_type == "Module" and not value.startswith("module:"):
            raise MapFailure("invalid_node_id", "Module 节点 ID 必须以 module: 开头。")
        if expected_type == "Concept" and not value.startswith("concept:"):
            raise MapFailure("invalid_node_id", "Concept 节点 ID 必须以 concept: 开头。")
        return value

    @staticmethod
    def _text(value: Any, name: str, maximum: int, *, required: bool = False) -> str:
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()):
            raise MapFailure("invalid_input", f"{name} 必须是非空字符串。" if required else f"{name} 必须是字符串。")
        if len(value) > maximum or "\x00" in value:
            raise MapFailure("invalid_input", f"{name} 不能超过 {maximum} 个字符或包含 NUL。")
        return value.strip() if required else value

    @classmethod
    def _parse_node(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise MapFailure("invalid_input", "upsert_nodes 每项必须是对象。")
        allowed = {"id", "type", "name", "summary", "aliases", "state", "evidence"}
        if set(raw) - allowed:
            raise MapFailure("invalid_input", f"节点包含未知字段：{', '.join(sorted(set(raw) - allowed))}")
        node_type = raw.get("type")
        if not isinstance(node_type, str) or node_type not in SEMANTIC_TYPES:
            raise MapFailure("managed_node", "update_map 只允许写入 Module 和 Concept 节点。")
        node_id = cls._node_id(raw.get("id"), node_type)
        name = cls._text(raw.get("name"), "name", MAX_NODE_NAME_CHARS, required=True)
        summary = cls._text(raw.get("summary", ""), "summary", MAX_NODE_SUMMARY_CHARS)
        state = raw.get("state")
        if not isinstance(state, str) or state not in {"tentative", "confirmed"}:
            raise MapFailure("invalid_state", "state 必须是 tentative 或 confirmed。")
        aliases = raw.get("aliases", [])
        if type(aliases) is not list or len(aliases) > MAX_ALIASES:
            raise MapFailure("invalid_aliases", f"aliases 必须是最多 {MAX_ALIASES} 项的数组。")
        clean_aliases = [cls._text(alias, "alias", MAX_ALIAS_CHARS, required=True) for alias in aliases]
        if len({alias.casefold() for alias in clean_aliases}) != len(clean_aliases):
            raise MapFailure("invalid_aliases", "aliases 中不能包含重复项（忽略大小写后比较）。")
        evidence = raw.get("evidence", [])
        if type(evidence) is not list:
            raise MapFailure("invalid_evidence", "evidence 必须是数组。")
        if len(evidence) > MAX_EVIDENCE_REFERENCES:
            raise MapFailure(
                "invalid_evidence", f"单个节点最多接受 {MAX_EVIDENCE_REFERENCES} 条依据。",
                field="evidence", evidence_references=len(evidence), evidence_limit=MAX_EVIDENCE_REFERENCES,
            )
        if state == "confirmed" and not evidence:
            raise MapFailure("confirmed_requires_evidence", "confirmed 节点必须提供至少一个当前有效的文件依据。")
        return {
            "id": node_id,
            "type": node_type,
            "name": name,
            "summary": summary,
            "aliases": clean_aliases,
            "state": state,
            "evidence": evidence,
        }

    @classmethod
    def _parse_edge(cls, raw: Any, *, deleting: bool = False) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise MapFailure("invalid_input", "边必须是对象。")
        allowed = {"source_id", "relation", "target_id"} if deleting else {
            "source_id", "relation", "target_id", "evidence", "roles"
        }
        if set(raw) - allowed:
            raise MapFailure("invalid_input", "边包含未知字段。")
        source = cls._node_id(raw.get("source_id"), allow_managed=True)
        target = cls._node_id(raw.get("target_id"), allow_managed=True)
        relation = raw.get("relation")
        if not isinstance(relation, str) or relation not in RELATION_TYPES:
            raise MapFailure("invalid_relation", "relation 必须是 contains、maps_to、depends_on 或 related_to。")
        if source == target:
            raise MapFailure("self_relation", "关系端点不能是同一个节点。")
        if relation == "related_to" and target < source:
            source, target = target, source
        evidence = [] if deleting else raw.get("evidence", [])
        if type(evidence) is not list:
            raise MapFailure("invalid_evidence", "边 evidence 必须是数组。")
        if len(evidence) > MAX_EVIDENCE_REFERENCES:
            raise MapFailure(
                "invalid_evidence", f"单条关系最多接受 {MAX_EVIDENCE_REFERENCES} 条依据。",
                field="evidence", evidence_references=len(evidence), evidence_limit=MAX_EVIDENCE_REFERENCES,
            )
        roles: list[str] = []
        if not deleting and relation == "maps_to":
            roles = raw.get("roles", ["unspecified"])
            valid_roles = {"implementation", "documentation", "test", "unspecified"}
            if type(roles) is not list or not roles or any(not isinstance(role, str) or role not in valid_roles for role in roles):
                raise MapFailure(
                    "invalid_roles",
                    "maps_to.roles 必须是 implementation、documentation、test、unspecified 中的一项或多项。",
                )
            roles = sorted(set(roles))
            if "unspecified" in roles and len(roles) > 1:
                raise MapFailure("invalid_roles", "unspecified 不能与其他角色同时使用。")
        elif not deleting and "roles" in raw:
            raise MapFailure("invalid_roles", "roles 只适用于 maps_to 关系。")
        return {
            "source_id": source, "target_id": target, "relation": relation,
            "evidence": evidence, "roles": roles,
        }

    def _verify_evidence(
        self,
        connection,
        project_id: str,
        project: ProjectFiles,
        secret: bytes,
        raw_items: list[Any],
        cache: dict[str, tuple[str, int, int | None, int | None]],
        budget: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict) or set(raw) != {"path", "version_token"}:
                raise MapFailure("invalid_evidence", "每项 evidence 必须只包含 path 和 version_token。")
            try:
                normalized = project._normalise_relative(raw["path"], allow_root=False)
                canonical, _ = project._path_for(normalized)
            except (ToolFailure, KeyError, TypeError) as exc:
                message = exc.message if isinstance(exc, ToolFailure) else "path 和 version_token 均为必填字段。"
                raise MapFailure("invalid_evidence", message) from exc
            token = raw["version_token"]
            if not isinstance(token, str) or not VERSION_TOKEN_RE.fullmatch(token):
                raise MapFailure("invalid_version_token", f"文件 {canonical} 的 version_token 格式无效。")
            if canonical in seen_paths:
                raise MapFailure("duplicate_evidence", f"同一 evidence 列表中重复引用了文件：{canonical}")
            seen_paths.add(canonical)
            entry = connection.execute(
                "SELECT node_id FROM files WHERE project_id = ? AND path = ? AND type = 'file'",
                (project_id, canonical),
            ).fetchone()
            if not entry or not entry["node_id"]:
                raise MapFailure(
                    "file_not_indexed", f"依据文件尚未进入清单：{canonical}。请先调用 refresh。", stale_files=[canonical]
                )
            cached = cache.get(canonical)
            if cached is None:
                if time.monotonic() - budget["started"] > budget.get("limit_seconds", MAX_UPDATE_SECONDS):
                    raise MapFailure("version_budget", "依据校验超过更新时间预算；整批未写入。")
                current = project.compute_version_token(
                    canonical, project_id, secret,
                    max_seconds=max(0.01, min(5.0, budget.get("limit_seconds", MAX_UPDATE_SECONDS) - (time.monotonic() - budget["started"]))),
                )
                budget["bytes"] += current.bytes_hashed
                if budget["bytes"] > budget.get("byte_limit", MAX_EVIDENCE_HASH_BYTES):
                    raise MapFailure("version_budget", "依据校验超过累计哈希预算；整批未写入。")
                if not current.token:
                    raise MapFailure(
                        "version_unavailable",
                        f"无法校验文件依据 {canonical}（{current.reason or 'unavailable'}）；整批未写入。",
                        stale_files=[canonical],
                    )
                cached = (current.token, current.bytes_hashed, current.mtime_ns, current.size)
                cache[canonical] = cached
            if not hmac.compare_digest(cached[0], token):
                raise MapFailure(
                    "stale_evidence",
                    f"文件自读取后已变化，需重新预览：{canonical}",
                    stale_files=[canonical],
                )
            prepared.append({
                "path": canonical,
                "version_token": token,
                "captured_mtime_ns": cached[2],
                "captured_size": cached[3],
            })
        return prepared

    @staticmethod
    def _replace_aliases(connection, project_id: str, node_id: str, aliases: list[str]) -> None:
        connection.execute(
            "DELETE FROM map_aliases WHERE project_id = ? AND node_id = ?", (project_id, node_id)
        )
        connection.executemany(
            "INSERT INTO map_aliases(project_id, node_id, alias, alias_fold) VALUES (?, ?, ?, ?)",
            ((project_id, node_id, alias, alias.casefold()) for alias in aliases),
        )

    @staticmethod
    def _insert_evidence(connection, project_id: str, owner_id: str, evidence: list[dict[str, Any]], *, edge: bool) -> None:
        if edge:
            connection.executemany(
                "INSERT INTO edge_evidence(project_id, edge_id, file_path, version_token, created_at, captured_mtime_ns, captured_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((project_id, owner_id, item["path"], item["version_token"], utc_now(),
                  item.get("captured_mtime_ns"), item.get("captured_size")) for item in evidence),
            )
        else:
            connection.executemany(
                "INSERT INTO node_evidence(project_id, node_id, file_path, version_token, created_at, captured_mtime_ns, captured_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((project_id, owner_id, item["path"], item["version_token"], utc_now(),
                  item.get("captured_mtime_ns"), item.get("captured_size")) for item in evidence),
            )

    @staticmethod
    def _allowed_relation(connection, project_id: str, edge: dict[str, Any]) -> None:
        source = connection.execute(
            "SELECT type FROM map_nodes WHERE project_id = ? AND node_id = ?",
            (project_id, edge["source_id"]),
        ).fetchone()
        target = connection.execute(
            "SELECT type FROM map_nodes WHERE project_id = ? AND node_id = ?",
            (project_id, edge["target_id"]),
        ).fetchone()
        if not source or not target:
            other = connection.execute(
                "SELECT project_id FROM map_nodes WHERE node_id IN (?, ?) AND project_id <> ? LIMIT 1",
                (edge["source_id"], edge["target_id"], project_id),
            ).fetchone()
            code = "cross_project_edge" if other else "missing_endpoint"
            raise MapFailure(code, "边的端点缺失或属于另一个项目。", source_id=edge["source_id"], target_id=edge["target_id"])
        pair = (source["type"], target["type"])
        if pair not in ALLOWED_ENDPOINTS[edge["relation"]]:
            raise MapFailure(
                "invalid_edge_types",
                f"{edge['relation']} 不允许从 {pair[0]} 指向 {pair[1]}。",
                source_type=pair[0], target_type=pair[1],
            )

    @staticmethod
    def _check_contains_cycles(connection, project_id: str) -> None:
        rows = connection.execute(
            "SELECT e.source_id, e.target_id FROM map_edges e "
            "JOIN map_nodes s ON s.project_id = e.project_id AND s.node_id = e.source_id "
            "JOIN map_nodes t ON t.project_id = e.project_id AND t.node_id = e.target_id "
            "WHERE e.project_id = ? AND e.relation = 'contains'",
            (project_id,),
        )
        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = defaultdict(int)
        for row in rows:
            adjacency[row["source_id"]].append(row["target_id"])
            indegree[row["target_id"]] += 1
            indegree.setdefault(row["source_id"], 0)
        pending = [node for node, degree in indegree.items() if degree == 0]
        visited = 0
        while pending:
            node = pending.pop()
            visited += 1
            for child in adjacency.get(node, []):
                indegree[child] -= 1
                if indegree[child] == 0:
                    pending.append(child)
        if visited != len(indegree):
            raise MapFailure("contains_cycle", "contains 结构不能形成环；整批未写入。")

    def update_map(
        self,
        project_id: str,
        *,
        upsert_nodes: Any = None,
        delete_node_ids: Any = None,
        upsert_edges: Any = None,
        delete_edges: Any = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if type(dry_run) is not bool:
            return {"ok": False, "error": "invalid_input", "message": "dry_run 必须是布尔值。", "project_id": project_id}
        changes = {
            "upsert_nodes": list(upsert_nodes) if type(upsert_nodes) is list else upsert_nodes,
            "delete_node_ids": list(delete_node_ids) if type(delete_node_ids) is list else delete_node_ids,
            "upsert_edges": list(upsert_edges) if type(upsert_edges) is list else upsert_edges,
            "delete_edges": list(delete_edges) if type(delete_edges) is list else delete_edges,
        }
        if not dry_run:
            return self._update_map_once(project_id, **changes)

        input_counts = {
            key: len(value) if type(value) is list else None for key, value in changes.items()
        }
        evidence_references = 0
        unique_paths: set[str] = set()
        project = self.projects.get(project_id)
        for operation in ("upsert_nodes", "upsert_edges"):
            values = changes[operation] if type(changes[operation]) is list else []
            for owner in values:
                items = owner.get("evidence", []) if isinstance(owner, dict) else []
                if type(items) is not list:
                    continue
                evidence_references += len(items)
                for item in items:
                    raw_path = item.get("path") if isinstance(item, dict) else None
                    if not isinstance(raw_path, str):
                        continue
                    canonical = raw_path
                    if project is not None:
                        try:
                            normalized = project._normalise_relative(raw_path, allow_root=False)
                            canonical, _ = project._path_for(normalized)
                        except ToolFailure:
                            pass
                    unique_paths.add(canonical)
        preflight_input = {
            "operation_counts": input_counts,
            "evidence_references": evidence_references,
            "unique_paths": len(unique_paths),
            "operation_limits": {
                "upsert_nodes": MAX_UPSERT_NODES, "delete_node_ids": MAX_DELETE_NODES,
                "upsert_edges": MAX_UPSERT_EDGES, "delete_edges": MAX_DELETE_EDGES,
            },
            "evidence_limit": MAX_EVIDENCE_REFERENCES,
        }

        errors: list[dict[str, Any]] = []
        total_bytes_hashed = 0
        total_files_checked = 0
        started = time.monotonic()
        attempts = 0

        def bounded(response: dict[str, Any]) -> dict[str, Any]:
            while _json_size(response) > MAX_MAP_OUTPUT_BYTES and response.get("errors"):
                response["errors"].pop()
                response["errors_complete"] = False
                response["errors_truncated"] = True
                response["stop_reason"] = "output_budget"
            return response

        while attempts < 50:
            if time.monotonic() - started >= MAX_UPDATE_SECONDS or total_bytes_hashed >= MAX_EVIDENCE_HASH_BYTES:
                return bounded({
                    "ok": False, "error": "preflight_incomplete", "valid": False,
                    "project_id": project_id, "dry_run": True, "errors": errors,
                    **preflight_input,
                    "errors_complete": False, "stop_reason": "preflight_budget",
                    "validation_work": {
                        "files_checked": total_files_checked, "bytes_hashed": total_bytes_hashed,
                        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                    },
                })
            result = self._update_map_once(
                project_id, **changes, dry_run=True,
                _preflight_timeout=max(0.01, MAX_UPDATE_SECONDS - (time.monotonic() - started)),
                _preflight_byte_budget=max(0, MAX_EVIDENCE_HASH_BYTES - total_bytes_hashed),
            )
            work = result.get("validation_work", {})
            total_bytes_hashed += int(work.get("bytes_hashed", 0) or 0)
            total_files_checked += int(work.get("files_checked", 0) or 0)
            if result.get("ok"):
                if not errors:
                    result["validation_work"]["aggregate_elapsed_ms"] = max(
                        0, int((time.monotonic() - started) * 1000)
                    )
                    return result
                return bounded({
                    "ok": False, "error": "preflight_errors", "valid": False,
                    "project_id": project_id, "dry_run": True, "errors": errors[:50],
                    **preflight_input,
                    "errors_complete": True, "validation_work": {
                        "files_checked": total_files_checked, "bytes_hashed": total_bytes_hashed,
                        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "hash_byte_limit": MAX_EVIDENCE_HASH_BYTES,
                        "time_limit_ms": int(MAX_UPDATE_SECONDS * 1000),
                    },
                })

            operation = result.get("operation")
            index = result.get("index")
            suggestion = {
                "invalid_node_id": "检查 ID 前缀及字符；稳定 ID 不能指向其他项目。",
                "invalid_evidence": "使用 preview 返回的精确相对路径和 version_token。",
                "file_not_indexed": "先 refresh 对应目录，再用 resolve_paths 获取 File ID。",
                "stale_evidence": "重新 preview 文件，并复核摘要和关系仍然成立后再提交。",
                "missing_endpoint": "先创建端点节点，或检查精确 File ID。",
                "invalid_edge_types": "调整关系类型或端点节点类型。",
                "contains_cycle": "移除会形成结构环的 contains 关系。",
                "version_budget": "按概念拆分批次并减少依据文件总量。",
            }.get(result.get("error"), "检查该操作及其引用；未通过前不要提交。")
            field = result.get("field") or {
                "invalid_node_id": "id",
                "invalid_state": "state",
                "invalid_aliases": "aliases",
                "invalid_roles": "roles",
                "invalid_relation": "relation",
                "invalid_evidence": "evidence",
                "invalid_version_token": "evidence.version_token",
                "stale_evidence": "evidence.version_token",
                "missing_endpoint": "source_id/target_id",
                "invalid_edge_types": "relation/source_id/target_id",
            }.get(result.get("error"), "operation")
            error = {
                "operation": operation or "batch",
                "index": index,
                "field": field,
                "error": result.get("error", "validation_error"),
                "message": result.get("message", "预检失败。"),
                "suggestion": suggestion,
            }
            diagnostics = {
                key: value for key, value in result.items()
                if key not in {"ok", "dry_run", "valid", "project_id", "error", "message", "operation", "index", "field", "validation_work"}
            }
            if diagnostics:
                error["details"] = diagnostics
            if result.get("stale_files"):
                error["paths"] = result["stale_files"]
            errors.append(error)
            errors = errors[:50]
            removable = False
            if operation in changes and type(index) is int and 0 <= index < len(changes[operation]):
                changes[operation].pop(index)
                removable = True
            elif operation == "batch" or not operation:
                break
            elif result.get("error") in {"version_budget", "map_store_error"}:
                break
            if not removable:
                break
            attempts += 1
        return bounded({
            "ok": False, "error": "preflight_errors", "valid": False,
            "project_id": project_id, "dry_run": True, "errors": errors[:50],
            **preflight_input,
            "errors_complete": False, "stop_reason": "error_limit" if attempts >= 50 else "dependent_checks_skipped",
            "unvalidated": ["依赖已移除的无效操作之验证", "错误列表上限以外的项目"],
            "validation_work": {
                "files_checked": total_files_checked, "bytes_hashed": total_bytes_hashed,
                "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                "hash_byte_limit": MAX_EVIDENCE_HASH_BYTES,
                "time_limit_ms": int(MAX_UPDATE_SECONDS * 1000),
            },
        })

    def _update_map_once(
        self,
        project_id: str,
        *,
        upsert_nodes: Any = None,
        delete_node_ids: Any = None,
        upsert_edges: Any = None,
        delete_edges: Any = None,
        dry_run: bool = False,
        _preflight_timeout: float | None = None,
        _preflight_byte_budget: int | None = None,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        budget = {
            "started": time.monotonic(), "bytes": 0,
            "limit_seconds": _preflight_timeout or MAX_UPDATE_SECONDS,
            "byte_limit": _preflight_byte_budget if _preflight_byte_budget is not None else MAX_EVIDENCE_HASH_BYTES,
        }
        try:
            if type(dry_run) is not bool:
                raise MapFailure("invalid_input", "dry_run 必须是布尔值。")
            def parse_items(value: Any, operation: str, maximum: int, parser) -> list[Any]:
                parsed = []
                for item_index, raw_item in enumerate(self._array(value, operation, maximum)):
                    try:
                        item = parser(raw_item)
                    except MapFailure as exc:
                        exc.details.update(operation=operation, index=item_index)
                        raise
                    if isinstance(item, dict):
                        item["_index"] = item_index
                    parsed.append(item)
                return parsed

            nodes = parse_items(upsert_nodes, "upsert_nodes", MAX_UPSERT_NODES, self._parse_node)
            delete_nodes = parse_items(delete_node_ids, "delete_node_ids", MAX_DELETE_NODES, self._node_id)
            edges = parse_items(upsert_edges, "upsert_edges", MAX_UPSERT_EDGES, self._parse_edge)
            deleted_edges = parse_items(
                delete_edges, "delete_edges", MAX_DELETE_EDGES,
                lambda item: self._parse_edge(item, deleting=True),
            )
            node_ids = [node["id"] for node in nodes]
            if len(set(node_ids)) != len(node_ids) or len(set(delete_nodes)) != len(delete_nodes):
                raise MapFailure("duplicate_node_operation", "同一批次不能重复操作节点 ID。")
            if set(node_ids) & set(delete_nodes):
                raise MapFailure("conflicting_node_operations", "同一节点不能在同一批次中同时 upsert 和 delete。")
            edge_keys = [(e["source_id"], e["relation"], e["target_id"]) for e in edges]
            delete_edge_keys = [(e["source_id"], e["relation"], e["target_id"]) for e in deleted_edges]
            if len(set(edge_keys)) != len(edge_keys) or len(set(delete_edge_keys)) != len(delete_edge_keys):
                raise MapFailure("duplicate_edge", "同一批次不能重复写入或删除相同关系。")
            if set(edge_keys) & set(delete_edge_keys):
                raise MapFailure("conflicting_edge_operations", "同一关系不能在同一批次中同时 upsert 和 delete。")
            evidence_count = sum(len(node["evidence"]) for node in nodes) + sum(len(edge["evidence"]) for edge in edges)
            if evidence_count > MAX_EVIDENCE_REFERENCES:
                raw_paths: set[str] = set()
                for owner in [*nodes, *edges]:
                    for item in owner["evidence"]:
                        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                            continue
                        raw_path = item["path"]
                        try:
                            normalized = project._normalise_relative(raw_path, allow_root=False)
                            canonical, _ = project._path_for(normalized)
                        except ToolFailure:
                            canonical = raw_path
                        raw_paths.add(canonical)
                raise MapFailure(
                    "invalid_evidence", f"单次 update_map 最多校验 {MAX_EVIDENCE_REFERENCES} 条文件依据。",
                    evidence_references=evidence_count, unique_paths=len(raw_paths),
                    evidence_limit=MAX_EVIDENCE_REFERENCES,
                )

            prepared_nodes: list[dict[str, Any]] = []
            prepared_edges: list[dict[str, Any]] = []
            version_cache: dict[str, tuple[str, int, int | None, int | None]] = {}
            secret = self.store.version_secret()
            with self.store.transaction(immediate=True) as connection:
                # Validate all file-version claims before changing the transaction's map rows.
                for node in nodes:
                    try:
                        node["evidence"] = self._verify_evidence(
                            connection, project_id, project, secret, node["evidence"], version_cache, budget
                        )
                    except MapFailure as exc:
                        exc.details.update(operation="upsert_nodes", index=node["_index"])
                        raise
                    prepared_nodes.append(node)
                for edge in edges:
                    try:
                        edge["evidence"] = self._verify_evidence(
                            connection, project_id, project, secret, edge["evidence"], version_cache, budget
                        )
                    except MapFailure as exc:
                        exc.details.update(operation="upsert_edges", index=edge["_index"])
                        raise
                    prepared_edges.append(edge)

                for node_index, node_id in enumerate(delete_nodes):
                    existing = connection.execute(
                        "SELECT type, managed FROM map_nodes WHERE project_id = ? AND node_id = ?",
                        (project_id, node_id),
                    ).fetchone()
                    if not existing:
                        raise MapFailure("missing_node", f"要删除的节点不存在：{node_id}", operation="delete_node_ids", index=node_index)
                    if existing["managed"] or existing["type"] not in SEMANTIC_TYPES:
                        raise MapFailure("managed_node", "Project 和 File 节点由服务维护，不能手动删除。", operation="delete_node_ids", index=node_index)
                for edge in deleted_edges:
                    existing = connection.execute(
                        "SELECT edge_id, managed FROM map_edges WHERE project_id = ? AND source_id = ? AND relation = ? AND target_id = ?",
                        (project_id, edge["source_id"], edge["relation"], edge["target_id"]),
                    ).fetchone()
                    if not existing:
                        raise MapFailure("missing_edge", "要删除的关系不存在。", operation="delete_edges", index=edge["_index"])
                    if existing["managed"]:
                        raise MapFailure("managed_edge", "项目与文件间的自动 contains 关系由服务维护。", operation="delete_edges", index=edge["_index"])
                    edge["edge_id"] = existing["edge_id"]

                for node in prepared_nodes:
                    existing = connection.execute(
                        "SELECT type, managed FROM map_nodes WHERE project_id = ? AND node_id = ?",
                        (project_id, node["id"]),
                    ).fetchone()
                    if existing and (existing["managed"] or existing["type"] != node["type"]):
                        raise MapFailure("node_identity_conflict", f"节点 ID 已用于不可修改或不同类型节点：{node['id']}", operation="upsert_nodes", index=node["_index"])

                for edge in prepared_edges:
                    if edge["source_id"] in delete_nodes or edge["target_id"] in delete_nodes:
                        raise MapFailure("missing_endpoint", "关系端点同批次被删除。", operation="upsert_edges", index=edge["_index"])

                for edge in deleted_edges:
                    connection.execute("DELETE FROM map_edges WHERE project_id = ? AND edge_id = ?", (project_id, edge["edge_id"]))
                for node_id in delete_nodes:
                    connection.execute("DELETE FROM map_nodes WHERE project_id = ? AND node_id = ?", (project_id, node_id))

                now = utc_now()
                for node in prepared_nodes:
                    connection.execute(
                        "DELETE FROM node_freshness WHERE project_id = ? AND node_id = ?",
                        (project_id, node["id"]),
                    )
                    connection.execute(
                        "INSERT INTO map_nodes(node_id, project_id, type, path, name, summary, aliases_json, name_fold, "
                        "summary_fold, state, managed, created_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
                        "ON CONFLICT(project_id, node_id) DO UPDATE SET name = excluded.name, summary = excluded.summary, "
                        "aliases_json = excluded.aliases_json, name_fold = excluded.name_fold, summary_fold = excluded.summary_fold, "
                        "state = excluded.state, updated_at = excluded.updated_at",
                        (node["id"], project_id, node["type"], node["name"], node["summary"],
                         json.dumps(node["aliases"], ensure_ascii=False), node["name"].casefold(),
                         node["summary"].casefold(), node["state"], now, now),
                    )
                    self._replace_aliases(connection, project_id, node["id"], node["aliases"])
                    connection.execute(
                        "DELETE FROM node_evidence WHERE project_id = ? AND node_id = ?", (project_id, node["id"])
                    )
                    self._insert_evidence(connection, project_id, node["id"], node["evidence"], edge=False)

                returned_edge_ids: list[str] = []
                for edge in prepared_edges:
                    try:
                        self._allowed_relation(connection, project_id, edge)
                    except MapFailure as exc:
                        exc.details.update(operation="upsert_edges", index=edge["_index"])
                        raise
                    existing = connection.execute(
                        "SELECT edge_id, managed FROM map_edges WHERE project_id = ? AND source_id = ? AND relation = ? AND target_id = ?",
                        (project_id, edge["source_id"], edge["relation"], edge["target_id"]),
                    ).fetchone()
                    if existing and existing["managed"]:
                        raise MapFailure("managed_edge", "项目与文件间的自动 contains 关系由服务维护。", operation="upsert_edges", index=edge["_index"])
                    edge_id = existing["edge_id"] if existing else uuid.uuid4().hex
                    if existing:
                        connection.execute(
                            "DELETE FROM edge_freshness WHERE project_id = ? AND edge_id = ?",
                            (project_id, edge_id),
                        )
                        connection.execute(
                            "UPDATE map_edges SET updated_at = ? WHERE project_id = ? AND edge_id = ?",
                            (now, project_id, edge_id),
                        )
                        connection.execute(
                            "DELETE FROM edge_evidence WHERE project_id = ? AND edge_id = ?", (project_id, edge_id)
                        )
                    else:
                        connection.execute(
                            "INSERT INTO map_edges(edge_id, project_id, source_id, relation, target_id, managed, created_at, updated_at, roles_json) "
                            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                            (edge_id, project_id, edge["source_id"], edge["relation"], edge["target_id"], now, now,
                             json.dumps(edge["roles"], ensure_ascii=False)),
                        )
                    if existing:
                        connection.execute(
                            "UPDATE map_edges SET roles_json = ? WHERE project_id = ? AND edge_id = ?",
                            (json.dumps(edge["roles"], ensure_ascii=False), project_id, edge_id),
                        )
                    self._insert_evidence(connection, project_id, edge_id, edge["evidence"], edge=True)
                    returned_edge_ids.append(edge_id)

                self._check_contains_cycles(connection, project_id)
                if time.monotonic() - budget["started"] > budget["limit_seconds"]:
                    raise MapFailure("version_budget", "update_map 超过总时间预算；整批未写入。")
                for path, (expected_token, _initial_bytes, _initial_mtime, _initial_size) in list(version_cache.items()):
                    current = project.compute_version_token(
                        path, project_id, secret,
                        max_seconds=max(0.01, min(5.0, budget["limit_seconds"] - (time.monotonic() - budget["started"]))),
                    )
                    budget["bytes"] += current.bytes_hashed
                    if budget["bytes"] > budget["byte_limit"] or time.monotonic() - budget["started"] > budget["limit_seconds"]:
                        raise MapFailure("version_budget", "提交前的文件版本复核超过预算；整批未写入。")
                    if not current.token or not hmac.compare_digest(current.token, expected_token):
                        raise MapFailure("stale_evidence", f"文件在更新提交期间发生变化：{path}", stale_files=[path])
                    version_cache[path] = (expected_token, current.bytes_hashed, current.mtime_ns, current.size)

                for owner in [*prepared_nodes, *prepared_edges]:
                    for item in owner["evidence"]:
                        _token, _bytes, captured_mtime_ns, captured_size = version_cache[item["path"]]
                        item["captured_mtime_ns"] = captured_mtime_ns
                        item["captured_size"] = captured_size

                if dry_run:
                    unique_paths = {
                        item["path"] for owner in [*prepared_nodes, *prepared_edges]
                        for item in owner["evidence"]
                    }
                    raise DryRunRollback({
                        "ok": True, "dry_run": True, "valid": True, "project_id": project_id,
                        "operation_counts": {
                            "upsert_nodes": len(nodes), "delete_nodes": len(delete_nodes),
                            "upsert_edges": len(edges), "delete_edges": len(deleted_edges),
                        },
                        "evidence_references": evidence_count,
                        "unique_paths": len(unique_paths),
                        "evidence_limit": MAX_EVIDENCE_REFERENCES,
                        "validation_work": {
                            "files_checked": len(version_cache), "bytes_hashed": budget["bytes"],
                            "hash_byte_limit": MAX_EVIDENCE_HASH_BYTES,
                            "attempt_hash_byte_limit": budget["byte_limit"],
                            "elapsed_ms": max(0, int((time.monotonic() - budget["started"]) * 1000)),
                            "time_limit_ms": int(MAX_UPDATE_SECONDS * 1000),
                        },
                        "errors": [], "errors_complete": True,
                    })

            return {
                "ok": True,
                "project_id": project_id,
                "upserted_node_ids": node_ids,
                "deleted_node_ids": delete_nodes,
                "upserted_edge_ids": returned_edge_ids,
                "deleted_edge_count": len(deleted_edges),
            }
        except DryRunRollback as exc:
            return exc.report
        except MapFailure as exc:
            response = {"ok": False, "error": exc.code, "message": str(exc), "project_id": project_id, **exc.details}
            if dry_run:
                response["dry_run"] = True
                response["valid"] = False
                response["validation_work"] = {
                    "files_checked": len(locals().get("version_cache", {})),
                    "bytes_hashed": budget["bytes"],
                    "hash_byte_limit": MAX_EVIDENCE_HASH_BYTES,
                    "attempt_hash_byte_limit": budget["byte_limit"],
                    "elapsed_ms": max(0, int((time.monotonic() - budget["started"]) * 1000)),
                    "time_limit_ms": int(MAX_UPDATE_SECONDS * 1000),
                }
            return response
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    def search(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        case_sensitive: bool = True,
        node_types: list[str] | None = None,
    ) -> dict[str, Any]:
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}"}
        if not isinstance(query, str) or not query or "\x00" in query or len(query) > 256:
            return {"ok": False, "error": "invalid_input", "message": "map 搜索 query 必须是 1–256 个字符且不能包含 NUL。"}
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0 or offset > MAX_MAP_SEARCH_OFFSET:
            return {"ok": False, "error": "invalid_input", "message": "limit 必须为正整数，offset 必须为非负整数。"}
        if type(case_sensitive) is not bool:
            return {"ok": False, "error": "invalid_input", "message": "case_sensitive 必须是布尔值。"}
        if node_types is not None and (
            type(node_types) is not list or not node_types
            or any(not isinstance(item, str) or item not in NODE_TYPES for item in node_types)
        ):
            return {
                "ok": False, "error": "invalid_input",
                "message": "node_types 必须是 Project、Module、Concept、File 中的一项或多项。",
                "project_id": project_id,
            }
        effective_limit = min(limit, MAX_MAP_SEARCH_LIMIT)
        query_value = query if case_sensitive else query.casefold()
        name_col = "name" if case_sensitive else "name_fold"
        summary_col = "summary" if case_sensitive else "summary_fold"
        alias_col = "alias" if case_sensitive else "alias_fold"
        where = (
            f"project_id = ? AND (instr({name_col}, ?) > 0 OR instr({summary_col}, ?) > 0 OR "
            f"EXISTS (SELECT 1 FROM map_aliases a WHERE a.project_id = map_nodes.project_id "
            f"AND a.node_id = map_nodes.node_id AND instr(a.{alias_col}, ?) > 0))"
        )
        params: list[Any] = [project_id, query_value, query_value, query_value]
        if node_types:
            where += " AND type IN (" + ",".join("?" for _ in node_types) + ")"
            params.extend(node_types)
        try:
            with self.store._connection() as connection:
                total = connection.execute(
                    f"SELECT COUNT(*) FROM map_nodes WHERE {where}",
                    params,
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT node_id, type, name, summary, aliases_json, state, path, name_fold, summary_fold FROM map_nodes WHERE "
                    f"{where} ORDER BY name_fold, name, node_id LIMIT ? OFFSET ?",
                    [*params, effective_limit, offset],
                ).fetchall()
                entries = []
                for row in rows:
                    aliases = json.loads(row["aliases_json"])
                    matched_fields = []
                    if query_value in (row["name"] if case_sensitive else row["name_fold"]):
                        matched_fields.append("name")
                    if query_value in (row["summary"] if case_sensitive else row["summary_fold"]):
                        matched_fields.append("summary")
                    if any(query_value in (alias if case_sensitive else alias.casefold()) for alias in aliases):
                        matched_fields.append("aliases")
                    entries.append({
                        "id": row["node_id"], "type": row["type"], "name": row["name"],
                        "summary": row["summary"], "aliases": aliases,
                        "state": row["state"], "path": row["path"], "project_id": project_id,
                        "matched_fields": matched_fields,
                    })
                result: dict[str, Any] = {
                    "ok": True, "mode": "map", "project_id": project_id, "query": query,
                    "node_types": node_types,
                    "limit": effective_limit, "offset": offset, "total": total, "results": entries,
                    "next_offset": offset + len(entries) if offset + len(entries) < total else None,
                    "truncated": offset + len(entries) < total, "reason": "result_limit" if offset + len(entries) < total else None,
                    "output_limited": False,
                }
                byte_limited = False
                while entries and _json_size(result) > MAX_MAP_OUTPUT_BYTES:
                    entries.pop()
                    byte_limited = True
                if byte_limited:
                    result["output_limited"] = True
                    result["truncated"] = True
                    result["reason"] = "output_budget"
                    result["next_offset"] = offset + len(entries) if offset + len(entries) < total else None
                return result
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    def _check_freshness(
        self,
        project_id: str,
        project: ProjectFiles,
        secret: bytes,
        evidence: list[dict[str, Any]],
        cache: dict[str, dict[str, Any]],
        budget: dict[str, Any],
    ) -> dict[str, Any]:
        if not evidence:
            return {
                "status": "unknown", "checked_files": 0, "total_files": 0,
                "bytes_hashed": 0, "complete": False, "reason": "no_evidence",
                "files": [],
            }
        files: list[dict[str, Any]] = []
        stale = False
        unknown = False
        for item in evidence:
            path = item["path"]
            expected = item["version_token"]
            current = cache.get(path)
            if current is None:
                if len(cache) >= MAX_FRESHNESS_FILES:
                    current = {"token": None, "reason": "file_count_budget", "bytes": 0}
                elif time.monotonic() - budget["started"] >= MAX_FRESHNESS_SECONDS:
                    current = {"token": None, "reason": "time_budget", "bytes": 0}
                elif budget["bytes"] >= MAX_FRESHNESS_BYTES:
                    current = {"token": None, "reason": "byte_budget", "bytes": 0}
                else:
                    remaining = MAX_FRESHNESS_BYTES - budget["bytes"]
                    try:
                        _canonical, disk_path = project._path_for(path)
                        size = disk_path.stat().st_size
                    except (ToolFailure, OSError):
                        size = 0
                    # Version checks read a file twice. Avoid starting a file
                    # that cannot fit the remaining total-work budget.
                    if size * 2 > remaining:
                        current = {"token": None, "reason": "byte_budget", "bytes": 0}
                    else:
                        remaining_time = max(0.01, MAX_FRESHNESS_SECONDS - (time.monotonic() - budget["started"]))
                        version = project.compute_version_token(
                            path, project_id, secret,
                            verify_twice=True, max_seconds=min(5.0, remaining_time),
                        )
                        budget["bytes"] += version.bytes_hashed
                        if budget["bytes"] > MAX_FRESHNESS_BYTES:
                            current = {"token": None, "reason": "byte_budget", "bytes": version.bytes_hashed}
                        else:
                            current = {"token": version.token, "reason": version.reason, "bytes": version.bytes_hashed}
                            cache[path] = current
            reason = current["reason"]
            if current["token"] and hmac.compare_digest(current["token"], expected):
                state = "fresh"
            elif reason == "not_found":
                state = "stale"
                stale = True
            elif current["token"]:
                state = "stale"
                stale = True
                reason = "content_changed"
            else:
                state = "unknown"
                unknown = True
            files.append({"path": path, "status": state, "reason": reason})
        status = "stale" if stale else "unknown" if unknown else "fresh"
        return {
            "status": status,
            "checked_files": sum(item["status"] != "unknown" for item in files),
            "total_files": len(evidence),
            "bytes_hashed": sum(cache.get(item["path"], {}).get("bytes", 0) for item in evidence),
            "complete": not unknown,
            "reason": "check_incomplete" if unknown else None,
            "files": files,
        }

    def verify_freshness(
        self, project_id: str, owner_type: str, owner_id: str
    ) -> dict[str, Any]:
        """Recheck one semantic node or edge and persist only its latest freshness observation."""
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        if owner_type not in {"node", "edge"}:
            return {"ok": False, "error": "invalid_input", "message": "owner_type 必须是 node 或 edge。", "project_id": project_id}
        if not isinstance(owner_id, str) or not NODE_ID_RE.fullmatch(owner_id):
            return {"ok": False, "error": "invalid_input", "message": "owner_id 格式无效。", "project_id": project_id}

        started = time.monotonic()
        cache: dict[str, dict[str, Any]] = {}
        budget = {"started": started, "bytes": 0}
        try:
            with self.store._connection() as connection:
                if owner_type == "node":
                    owner = connection.execute(
                        "SELECT type FROM map_nodes WHERE project_id = ? AND node_id = ?",
                        (project_id, owner_id),
                    ).fetchone()
                    if owner is None:
                        return {"ok": False, "error": "node_not_found", "message": f"未找到节点：{owner_id}", "project_id": project_id}
                    if owner["type"] not in SEMANTIC_TYPES:
                        return {"ok": False, "error": "invalid_input", "message": "只能核验 Module 或 Concept 节点。", "project_id": project_id}
                    total = connection.execute(
                        "SELECT COUNT(*) FROM node_evidence WHERE project_id = ? AND node_id = ?",
                        (project_id, owner_id),
                    ).fetchone()[0]
                    evidence = [dict(row) for row in connection.execute(
                        "SELECT file_path AS path, version_token FROM node_evidence "
                        "WHERE project_id = ? AND node_id = ? ORDER BY file_path LIMIT 33",
                        (project_id, owner_id),
                    )]
                    table, key_column = "node_freshness", "node_id"
                else:
                    owner = connection.execute(
                        "SELECT managed FROM map_edges WHERE project_id = ? AND edge_id = ?",
                        (project_id, owner_id),
                    ).fetchone()
                    if owner is None:
                        return {"ok": False, "error": "edge_not_found", "message": f"未找到关系：{owner_id}", "project_id": project_id}
                    if owner["managed"]:
                        return {"ok": False, "error": "invalid_input", "message": "服务管理的结构关系没有可核验的语义依据。", "project_id": project_id}
                    total = connection.execute(
                        "SELECT COUNT(*) FROM edge_evidence WHERE project_id = ? AND edge_id = ?",
                        (project_id, owner_id),
                    ).fetchone()[0]
                    evidence = [dict(row) for row in connection.execute(
                        "SELECT file_path AS path, version_token FROM edge_evidence "
                        "WHERE project_id = ? AND edge_id = ? ORDER BY file_path LIMIT 33",
                        (project_id, owner_id),
                    )]
                    table, key_column = "edge_freshness", "edge_id"

                if total > MAX_EVIDENCE_REFERENCES:
                    freshness = {
                        "status": "unknown", "checked_files": 0, "total_files": total,
                        "bytes_hashed": 0, "complete": False,
                        "reason": "evidence_count_limit", "files": [],
                    }
                else:
                    freshness = self._check_freshness(
                        project_id, self.projects[project_id], self.store.version_secret(),
                        evidence, cache, budget,
                    )
                observed_at = utc_now()
                freshness["observed_at"] = observed_at
                connection.execute(
                    f"INSERT INTO {table}(project_id, {key_column}, status, observed_at, stale_paths_json, "
                    "checked_files, total_files, bytes_hashed, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(project_id, {key_column}) DO UPDATE SET status=excluded.status, "
                    "observed_at=excluded.observed_at, stale_paths_json=excluded.stale_paths_json, "
                    "checked_files=excluded.checked_files, total_files=excluded.total_files, "
                    "bytes_hashed=excluded.bytes_hashed, reason=excluded.reason",
                    (project_id, owner_id, freshness["status"], observed_at,
                     json.dumps([item["path"] for item in freshness["files"] if item["status"] == "stale"], ensure_ascii=False),
                     freshness["checked_files"], freshness["total_files"],
                     freshness["bytes_hashed"], freshness["reason"]),
                )
                return {
                    "ok": True, "project_id": project_id, "owner_type": owner_type,
                    "owner_id": owner_id, "freshness": freshness,
                    "freshness_check": {
                        "files_checked": len(cache), "file_limit": MAX_FRESHNESS_FILES,
                        "bytes_hashed": budget["bytes"], "byte_limit": MAX_FRESHNESS_BYTES,
                        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "time_limit_ms": int(MAX_FRESHNESS_SECONDS * 1000),
                    },
                }
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    @staticmethod
    def _change_owners(
        connection: sqlite3.Connection,
        project_id: str,
        path: str,
        *,
        offset: int,
        limit: int,
        current_token: str | None,
        current_reason: str | None,
        current_mtime_ns: int | None,
        current_size: int | None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = connection.execute(
            "WITH owners AS ("
            "SELECT 'node' AS kind, n.node_id AS owner_id, n.name AS owner_name, n.type AS node_type, "
            "NULL AS relation, NULL AS source_id, NULL AS source_name, NULL AS target_id, NULL AS target_name, "
            "NULL AS roles_json, e.created_at, e.captured_mtime_ns, e.captured_size, e.version_token "
            "FROM node_evidence e JOIN map_nodes n ON n.project_id=e.project_id AND n.node_id=e.node_id "
            "WHERE e.project_id=? AND e.file_path=? "
            "UNION ALL "
            "SELECT 'edge', e.edge_id, s.name || ' → ' || t.name, s.type, m.relation, m.source_id, s.name, "
            "m.target_id, t.name, m.roles_json, e.created_at, e.captured_mtime_ns, e.captured_size, e.version_token "
            "FROM edge_evidence e JOIN map_edges m ON m.project_id=e.project_id AND m.edge_id=e.edge_id "
            "JOIN map_nodes s ON s.project_id=m.project_id AND s.node_id=m.source_id "
            "JOIN map_nodes t ON t.project_id=m.project_id AND t.node_id=m.target_id "
            "WHERE e.project_id=? AND e.file_path=? "
            "UNION ALL "
            "SELECT 'maps_to', m.edge_id, s.name, s.type, m.relation, m.source_id, s.name, m.target_id, "
            "t.name, m.roles_json, NULL, NULL, NULL, NULL "
            "FROM map_edges m JOIN map_nodes s ON s.project_id=m.project_id AND s.node_id=m.source_id "
            "JOIN map_nodes t ON t.project_id=m.project_id AND t.node_id=m.target_id "
            "WHERE m.project_id=? AND m.relation='maps_to' AND t.type='File' AND t.path=?"
            ") SELECT *, COUNT(*) OVER() AS owner_total FROM owners "
            "ORDER BY CASE kind WHEN 'node' THEN 0 WHEN 'edge' THEN 1 ELSE 2 END, owner_name, owner_id "
            "LIMIT ? OFFSET ?",
            (project_id, path, project_id, path, project_id, path, limit, offset),
        ).fetchall()
        if rows:
            total = int(rows[0]["owner_total"])
        else:
            total = int(connection.execute(
                "SELECT (SELECT COUNT(*) FROM node_evidence WHERE project_id=? AND file_path=?) + "
                "(SELECT COUNT(*) FROM edge_evidence WHERE project_id=? AND file_path=?) + "
                "(SELECT COUNT(*) FROM map_edges m JOIN map_nodes t ON t.project_id=m.project_id AND t.node_id=m.target_id "
                "WHERE m.project_id=? AND m.relation='maps_to' AND t.type='File' AND t.path=?)",
                (project_id, path, project_id, path, project_id, path),
            ).fetchone()[0])

        owners: list[dict[str, Any]] = []
        for row in rows:
            kind = row["kind"]
            owner: dict[str, Any] = {
                "owner_type": kind,
                "owner_id": row["owner_id"],
                "name": row["owner_name"],
                "node_type": row["node_type"],
            }
            if kind == "maps_to":
                owner.update({
                    "status": "navigation_only",
                    "relation": "maps_to",
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "target_id": row["target_id"],
                    "target_name": row["target_name"],
                    "direction": "Concept → File",
                    "roles": json.loads(row["roles_json"] or '["unspecified"]'),
                })
            else:
                if current_token is None:
                    status = "missing" if current_reason == "not_found" else "unknown"
                else:
                    if not hmac.compare_digest(row["version_token"], current_token):
                        status = "content_changed"
                    elif row["captured_mtime_ns"] is not None and (
                        row["captured_mtime_ns"] != current_mtime_ns
                        or (row["captured_size"] is not None and row["captured_size"] != current_size)
                    ):
                        status = "metadata_only"
                    else:
                        status = "unchanged"
                owner.update({
                    "status": status,
                    "relation": row["relation"],
                    "direction": "Concept → File" if row["relation"] == "maps_to" else "source → target",
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "target_id": row["target_id"],
                    "target_name": row["target_name"],
                    "evidence_created_at": row["created_at"],
                    "captured_mtime_ns": row["captured_mtime_ns"],
                    "captured_size": row["captured_size"],
                    "time_baseline_available": row["captured_mtime_ns"] is not None,
                })
                if current_token is None:
                    owner["reason"] = current_reason
            owners.append(owner)
        return owners, total

    def review_changes(
        self,
        project_id: str,
        *,
        directory: str = "",
        path: str = "",
        limit: int = 20,
        offset: int = 0,
        owner_offset: int = 0,
        owner_limit: int = 5,
        include_unchanged: bool = False,
    ) -> dict[str, Any]:
        """Read-only, bounded content-version check for directly referenced files."""
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        if (type(limit) is not int or limit < 1 or type(offset) is not int
                or offset < 0 or offset > MAX_MAP_SEARCH_OFFSET):
            return {"ok": False, "error": "invalid_input", "message": "limit 必须为正整数，offset 必须为非负整数。", "project_id": project_id}
        if (type(owner_offset) is not int or owner_offset < 0 or owner_offset > MAX_MAP_SEARCH_OFFSET
                or type(owner_limit) is not int or owner_limit < 1):
            return {"ok": False, "error": "invalid_input", "message": "owner_offset 必须为非负整数，owner_limit 必须为正整数。", "project_id": project_id}
        if type(include_unchanged) is not bool:
            return {"ok": False, "error": "invalid_input", "message": "include_unchanged 必须为布尔值。", "project_id": project_id}
        if path and directory:
            return {"ok": False, "error": "invalid_input", "message": "path 与 directory 不能同时指定。", "project_id": project_id}
        effective_limit = min(limit, MAX_CHANGE_PAGE_FILES)
        effective_owner_limit = min(owner_limit, MAX_CHANGE_OWNER_PAGE)
        project = self.projects[project_id]
        try:
            normalized_directory = project._normalise_relative(directory, allow_root=True) if directory else ""
            normalized_path = project._normalise_relative(path, allow_root=False) if path else ""
        except ToolFailure as exc:
            return project._failure(exc)

        started = time.monotonic()
        work = {"bytes_hashed": 0, "files_checked": 0, "stop_reason": None}
        try:
            secret = self.store.version_secret()
            with self.store._connection() as connection:
                sources = (
                    "SELECT file_path AS path FROM node_evidence WHERE project_id=? "
                    "UNION SELECT file_path AS path FROM edge_evidence WHERE project_id=? "
                    "UNION SELECT t.path AS path FROM map_edges m "
                    "JOIN map_nodes t ON t.project_id=m.project_id AND t.node_id=m.target_id "
                    "WHERE m.project_id=? AND m.relation='maps_to' AND t.type='File'"
                )
                filter_sql = ""
                params: list[Any] = [project_id, project_id, project_id]
                if normalized_path:
                    filter_sql = " WHERE path=?"
                    params.append(normalized_path)
                elif normalized_directory:
                    prefix = normalized_directory + "/"
                    filter_sql = " WHERE path=? OR substr(path,1,?)=?"
                    params.extend([normalized_directory, len(prefix), prefix])
                total = int(connection.execute(
                    f"SELECT COUNT(*) FROM (SELECT path FROM ({sources}) {filter_sql} GROUP BY path)", params
                ).fetchone()[0])
                paths = [row["path"] for row in connection.execute(
                    f"SELECT path FROM ({sources}) {filter_sql} GROUP BY path ORDER BY path LIMIT ? OFFSET ?",
                    [*params, effective_limit, offset],
                ).fetchall()]
                if normalized_path and total == 0:
                    return {"ok": False, "error": "unreferenced_path", "message": "该文件没有直接 Evidence 或 maps_to 关联。", "project_id": project_id, "path": normalized_path}

                entries: list[dict[str, Any]] = []
                consumed = 0
                for candidate_path in paths:
                    elapsed = time.monotonic() - started
                    if elapsed >= MAX_CHANGE_SECONDS:
                        work["stop_reason"] = "time_budget"
                        break
                    evidence_counts = connection.execute(
                        "SELECT (SELECT COUNT(*) FROM node_evidence WHERE project_id=? AND file_path=?) AS nodes, "
                        "(SELECT COUNT(*) FROM edge_evidence WHERE project_id=? AND file_path=?) AS edges",
                        (project_id, candidate_path, project_id, candidate_path),
                    ).fetchone()
                    evidence_total = int(evidence_counts["nodes"] + evidence_counts["edges"])
                    mapping_total = int(connection.execute(
                        "SELECT COUNT(*) FROM map_edges m JOIN map_nodes t "
                        "ON t.project_id=m.project_id AND t.node_id=m.target_id "
                        "WHERE m.project_id=? AND m.relation='maps_to' AND t.type='File' AND t.path=?",
                        (project_id, candidate_path),
                    ).fetchone()[0])

                    current = None
                    current_mtime_ns = None
                    current_size = None
                    manifest = connection.execute(
                        "SELECT mtime_ns,size,indexed_at FROM files WHERE project_id=? AND path=? AND type='file'",
                        (project_id, candidate_path),
                    ).fetchone()
                    try:
                        _canonical, disk_path = project._path_for(candidate_path)
                        info = disk_path.lstat()
                        if project._is_reparse_or_symlink(info, disk_path) or not stat.S_ISREG(info.st_mode):
                            raise ToolFailure("not_regular_file", "目标不是允许读取的普通文件。")
                        current_mtime_ns = int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)))
                        current_size = int(info.st_size)
                    except ToolFailure as exc:
                        current = {"token": None, "reason": exc.code}
                    except OSError:
                        current = {"token": None, "reason": "unavailable"}

                    if evidence_total and current is None:
                        remaining_bytes = MAX_CHANGE_BYTES - work["bytes_hashed"]
                        if current_size * 2 > remaining_bytes:
                            current = {"token": None, "reason": "byte_budget"}
                            work["stop_reason"] = "byte_budget"
                        else:
                            remaining_seconds = MAX_CHANGE_SECONDS - (time.monotonic() - started)
                            if remaining_seconds <= 0:
                                work["stop_reason"] = "time_budget"
                                break
                            version = project.compute_version_token(
                                candidate_path, project_id, secret, verify_twice=True,
                                max_seconds=min(5.0, remaining_seconds),
                            )
                            work["bytes_hashed"] += version.bytes_hashed
                            current = {"token": version.token, "reason": version.reason}
                            if version.mtime_ns is not None:
                                current_mtime_ns = version.mtime_ns
                            if version.size is not None:
                                current_size = version.size
                            if not version.token and version.reason in {"hash_timeout", "file_too_large"}:
                                work["stop_reason"] = "time_budget" if version.reason == "hash_timeout" else "file_budget"
                    work["files_checked"] += 1

                    if evidence_total:
                        if current and current.get("token"):
                            stale_count = int(connection.execute(
                                "SELECT COUNT(*) FROM (SELECT version_token FROM node_evidence WHERE project_id=? AND file_path=? "
                                "UNION ALL SELECT version_token FROM edge_evidence WHERE project_id=? AND file_path=?) "
                                "WHERE version_token<>?",
                                (project_id, candidate_path, project_id, candidate_path, current["token"]),
                            ).fetchone()[0])
                            metadata_diff_count = int(connection.execute(
                                "SELECT COUNT(*) FROM (SELECT version_token,captured_mtime_ns,captured_size FROM node_evidence "
                                "WHERE project_id=? AND file_path=? UNION ALL SELECT version_token,captured_mtime_ns,captured_size "
                                "FROM edge_evidence WHERE project_id=? AND file_path=?) WHERE version_token=? AND "
                                "((captured_mtime_ns IS NOT NULL AND captured_mtime_ns<>?) OR "
                                "(captured_size IS NOT NULL AND captured_size<>?))",
                                (project_id, candidate_path, project_id, candidate_path,
                                 current["token"], current_mtime_ns, current_size),
                            ).fetchone()[0])
                            baseline_missing = int(connection.execute(
                                "SELECT COUNT(*) FROM (SELECT captured_mtime_ns,captured_size FROM node_evidence "
                                "WHERE project_id=? AND file_path=? UNION ALL SELECT captured_mtime_ns,captured_size "
                                "FROM edge_evidence WHERE project_id=? AND file_path=?) "
                                "WHERE captured_mtime_ns IS NULL OR captured_size IS NULL",
                                (project_id, candidate_path, project_id, candidate_path),
                            ).fetchone()[0])
                            if stale_count:
                                status, reason = "content_changed", "content_changed"
                            elif metadata_diff_count:
                                status, reason = "metadata_only", "metadata_changed"
                            else:
                                status, reason = "unchanged", None
                            if current_mtime_ns is None:
                                status, reason = "unknown", current.get("reason") or "metadata_unavailable"
                        else:
                            reason = (current or {}).get("reason") or "unavailable"
                            status = "missing" if reason == "not_found" else "unknown"
                            stale_count = 0
                            metadata_diff_count = 0
                            baseline_missing = evidence_total
                    else:
                        stale_count = 0
                        metadata_diff_count = 0
                        baseline_missing = 0
                        if current_mtime_ns is None:
                            reason = (current or {}).get("reason") or "not_found"
                            status = "missing" if reason == "not_found" else "unknown"
                        elif manifest is None or manifest["mtime_ns"] is None:
                            status, reason = "no_evidence", "no_content_baseline"
                        elif current_mtime_ns != manifest["mtime_ns"] or current_size != manifest["size"]:
                            status, reason = "metadata_since_refresh", "no_content_baseline"
                        else:
                            status, reason = "no_evidence", "no_content_baseline"

                    owners, owner_total = self._change_owners(
                        connection, project_id, candidate_path,
                        offset=owner_offset if normalized_path else 0,
                        limit=effective_owner_limit,
                        current_token=current.get("token") if current else None,
                        current_reason=current.get("reason") if current else None,
                        current_mtime_ns=current_mtime_ns,
                        current_size=current_size,
                    )
                    needs_attention = status not in {"unchanged", "no_evidence"}
                    consumed += 1
                    if include_unchanged or needs_attention or normalized_path:
                        entries.append({
                            "path": candidate_path,
                            "status": status,
                            "reason": reason,
                            "current_mtime_ns": current_mtime_ns,
                            "current_size": current_size,
                            "manifest_mtime_ns": manifest["mtime_ns"] if manifest else None,
                            "manifest_indexed_at": manifest["indexed_at"] if manifest else None,
                            "evidence_reference_count": evidence_total,
                            "stale_evidence_reference_count": stale_count,
                            "metadata_changed_reference_count": metadata_diff_count,
                            "time_baseline_missing_count": baseline_missing,
                            "maps_to_count": mapping_total,
                            "owner_total": owner_total,
                            "owners": owners,
                            "owner_offset": owner_offset if normalized_path else 0,
                            "owner_limit": effective_owner_limit,
                            "owner_next_offset": (owner_offset if normalized_path else 0) + len(owners)
                            if (owner_offset if normalized_path else 0) + len(owners) < owner_total else None,
                            "owner_complete": (owner_offset if normalized_path else 0) + len(owners) >= owner_total,
                            "needs_attention": needs_attention,
                            "_candidate_offset": offset + consumed - 1,
                        })
                    if work["stop_reason"]:
                        break

                if normalized_path:
                    next_offset = None
                    complete = work["stop_reason"] is None
                    page_total = 1
                else:
                    next_offset = offset + consumed if offset + consumed < total else None
                    complete = next_offset is None and work["stop_reason"] is None
                response: dict[str, Any] = {
                    "ok": True,
                    "project_id": project_id,
                    "scope": normalized_directory or ".",
                    "path": normalized_path or None,
                    "checked_at": utc_now(),
                    "read_only": True,
                    "include_unchanged": include_unchanged,
                    "limit": effective_limit,
                    "offset": offset,
                    "total_paths": total,
                    "checked_paths": consumed,
                    "returned_paths": len(entries),
                    "entries": entries,
                    "next_offset": next_offset,
                    "complete": complete,
                    "stop_reason": work["stop_reason"],
                    "owner_limit": effective_owner_limit,
                    "owner_offset": owner_offset,
                    "work": {
                        "files_checked": work["files_checked"],
                        "bytes_hashed": work["bytes_hashed"],
                        "byte_limit": MAX_CHANGE_BYTES,
                        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "time_limit_ms": int(MAX_CHANGE_SECONDS * 1000),
                    },
                    "counts": {
                        "needs_attention": sum(item["needs_attention"] for item in entries),
                        "unchanged_returned": sum(item["status"] == "unchanged" for item in entries),
                        "metadata_since_refresh": sum(item["status"] == "metadata_since_refresh" for item in entries),
                    },
                }
                if normalized_path and entries and _json_size(response) > MAX_MAP_OUTPUT_BYTES:
                    response["output_limited"] = True
                    response["complete"] = False
                    response["stop_reason"] = "output_budget"
                    while entries[0]["owners"] and _json_size(response) > MAX_MAP_OUTPUT_BYTES:
                        entries[0]["owners"].pop()
                        entries[0]["owner_next_offset"] = owner_offset + len(entries[0]["owners"])
                        entries[0]["owner_complete"] = False
                while not normalized_path and entries and _json_size(response) > MAX_MAP_OUTPUT_BYTES:
                    removed = entries.pop()
                    response["returned_paths"] = len(entries)
                    response["output_limited"] = True
                    response["complete"] = False
                    response["stop_reason"] = "output_budget"
                    response["checked_paths"] = removed["_candidate_offset"] - offset
                    response["next_offset"] = removed["_candidate_offset"]
                for item in entries:
                    item.pop("_candidate_offset", None)
                response["counts"] = {
                    "needs_attention": sum(item["needs_attention"] for item in entries),
                    "unchanged_returned": sum(item["status"] == "unchanged" for item in entries),
                    "metadata_since_refresh": sum(item["status"] == "metadata_since_refresh" for item in entries),
                }
                response.setdefault("output_limited", False)
                return response
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    def context(
        self,
        project_id: str,
        node_id: str,
        *,
        neighbor_limit: int = 10,
        evidence_offset: int = 0,
        evidence_limit: int = 20,
        check_freshness: bool = True,
    ) -> dict[str, Any]:
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}"}
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            return {"ok": False, "error": "invalid_node_id", "message": "node_id 格式无效。"}
        if type(neighbor_limit) is not int or neighbor_limit < 0:
            return {"ok": False, "error": "invalid_input", "message": "neighbor_limit 必须是非负整数。"}
        if type(evidence_offset) is not int or evidence_offset < 0:
            return {"ok": False, "error": "invalid_input", "message": "evidence_offset 必须是非负整数。", "project_id": project_id}
        if type(evidence_limit) is not int or evidence_limit < 1:
            return {"ok": False, "error": "invalid_input", "message": "evidence_limit 必须是正整数。", "project_id": project_id}
        if type(check_freshness) is not bool:
            return {"ok": False, "error": "invalid_input", "message": "check_freshness 必须是布尔值。", "project_id": project_id}
        effective_limit = min(neighbor_limit, MAX_CONTEXT_NEIGHBORS)
        effective_evidence_limit = min(evidence_limit, MAX_CONTEXT_EVIDENCE)
        try:
            with self.store._connection() as connection:
                row = connection.execute(
                    "SELECT node_id, type, name, summary, aliases_json, state, path, created_at, updated_at FROM map_nodes "
                    "WHERE project_id = ? AND node_id = ?",
                    (project_id, node_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "node_not_found", "message": f"未找到节点：{node_id}", "project_id": project_id}
                node = {
                    "id": row["node_id"], "type": row["type"], "name": row["name"],
                    "summary": row["summary"], "aliases": json.loads(row["aliases_json"]),
                    "state": row["state"], "path": row["path"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                }
                node_evidence_total = connection.execute(
                    "SELECT COUNT(*) FROM node_evidence WHERE project_id = ? AND node_id = ?",
                    (project_id, node_id),
                ).fetchone()[0]
                all_node_evidence = [dict(item) for item in connection.execute(
                    "SELECT file_path AS path, version_token, created_at FROM node_evidence "
                    "WHERE project_id = ? AND node_id = ? ORDER BY file_path LIMIT 33",
                    (project_id, node_id),
                )]
                node_evidence = [dict(item) for item in connection.execute(
                    "SELECT file_path AS path, version_token, created_at FROM node_evidence "
                    "WHERE project_id = ? AND node_id = ? ORDER BY file_path LIMIT ? OFFSET ?",
                    (project_id, node_id, effective_evidence_limit, evidence_offset),
                )]
                neighbor_total = connection.execute(
                    "SELECT COUNT(*) FROM map_edges WHERE project_id = ? AND (source_id = ? OR target_id = ?)",
                    (project_id, node_id, node_id),
                ).fetchone()[0]
                all_edges = connection.execute(
                    "SELECT edge_id, source_id, relation, target_id, roles_json, managed FROM map_edges "
                    "WHERE project_id = ? AND (source_id = ? OR target_id = ?) "
                    "ORDER BY relation, source_id, target_id LIMIT ? OFFSET 0",
                    (project_id, node_id, node_id, effective_limit),
                ).fetchall()
                neighbors = []
                files: dict[str, dict[str, Any]] = {}
                if node["type"] == "File" and node["path"]:
                    info = connection.execute(
                        "SELECT size, mtime_ns, indexed_at FROM files WHERE project_id = ? AND path = ? AND type = 'file'",
                        (project_id, node["path"]),
                    ).fetchone()
                    files[node["path"]] = {
                        "node_id": node_id, "path": node["path"], "type": "File",
                        "size": info["size"] if info else None,
                        "mtime_ns": info["mtime_ns"] if info else None,
                        "indexed_at": info["indexed_at"] if info else None,
                    }
                for evidence in node_evidence:
                    info = connection.execute(
                        "SELECT node_id, size, mtime_ns, indexed_at FROM files WHERE project_id = ? AND path = ? AND type = 'file'",
                        (project_id, evidence["path"]),
                    ).fetchone()
                    files[evidence["path"]] = {
                        "node_id": info["node_id"] if info else None,
                        "path": evidence["path"], "type": "File" if info else "missing_file",
                        "size": info["size"] if info else None,
                        "mtime_ns": info["mtime_ns"] if info else None,
                        "indexed_at": info["indexed_at"] if info else None,
                    }
                freshness_cache: dict[str, dict[str, Any]] = {}
                freshness_budget = {"started": time.monotonic(), "bytes": 0}
                freshness_secret = self.store.version_secret() if check_freshness else None

                def cached_freshness(table: str, key_column: str, key: str, total_files: int) -> dict[str, Any]:
                    cached = connection.execute(
                        f"SELECT status, observed_at, stale_paths_json, checked_files, total_files, bytes_hashed, reason "
                        f"FROM {table} WHERE project_id = ? AND {key_column} = ?",
                        (project_id, key),
                    ).fetchone()
                    if cached is None:
                        return {
                            "status": "unknown", "checked_files": 0, "total_files": total_files,
                            "bytes_hashed": 0, "complete": False,
                            "reason": "no_evidence" if total_files == 0 else "not_checked",
                            "observed_at": None, "stale_paths": [], "files": [],
                        }
                    return {
                        "status": cached["status"], "checked_files": cached["checked_files"],
                        "total_files": cached["total_files"], "bytes_hashed": cached["bytes_hashed"],
                        "complete": cached["reason"] is None, "reason": cached["reason"],
                        "observed_at": cached["observed_at"],
                        "stale_paths": json.loads(cached["stale_paths_json"]), "files": [],
                    }

                node_freshness = (
                    self._check_freshness(
                        project_id, self.projects[project_id], freshness_secret,
                        all_node_evidence, freshness_cache, freshness_budget,
                    ) if node_evidence_total <= MAX_EVIDENCE_REFERENCES else {
                        "status": "unknown", "checked_files": 0, "total_files": node_evidence_total,
                        "bytes_hashed": 0, "complete": False, "reason": "evidence_count_limit", "files": [],
                    }
                ) if check_freshness else cached_freshness("node_freshness", "node_id", node_id, node_evidence_total)
                node_observed_at = utc_now() if check_freshness and node["type"] in SEMANTIC_TYPES else None
                if node_observed_at:
                    node_freshness["observed_at"] = node_observed_at
                if check_freshness and node["type"] in SEMANTIC_TYPES:
                    connection.execute(
                        "INSERT INTO node_freshness(project_id, node_id, status, observed_at, stale_paths_json, "
                        "checked_files, total_files, bytes_hashed, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(project_id, node_id) DO UPDATE SET status=excluded.status, "
                        "observed_at=excluded.observed_at, stale_paths_json=excluded.stale_paths_json, "
                        "checked_files=excluded.checked_files, total_files=excluded.total_files, "
                        "bytes_hashed=excluded.bytes_hashed, reason=excluded.reason",
                        (project_id, node_id, node_freshness["status"], node_observed_at,
                         json.dumps([item["path"] for item in node_freshness["files"] if item["status"] == "stale"], ensure_ascii=False),
                         node_freshness["checked_files"], node_freshness["total_files"],
                         node_freshness["bytes_hashed"], node_freshness["reason"]),
                    )
                for edge in all_edges:
                    outgoing = edge["source_id"] == node_id
                    peer_id = edge["target_id"] if outgoing else edge["source_id"]
                    peer = connection.execute(
                        "SELECT node_id, type, name, summary, path, created_at, updated_at FROM map_nodes "
                        "WHERE project_id = ? AND node_id = ?", (project_id, peer_id)
                    ).fetchone()
                    if not peer:
                        continue
                    direction = "undirected" if edge["relation"] == "related_to" else ("outgoing" if outgoing else "incoming")
                    edge_evidence_total = connection.execute(
                        "SELECT COUNT(*) FROM edge_evidence WHERE project_id = ? AND edge_id = ?",
                        (project_id, edge["edge_id"]),
                    ).fetchone()[0]
                    all_edge_evidence = [dict(item) for item in connection.execute(
                        "SELECT file_path AS path, version_token, created_at FROM edge_evidence "
                        "WHERE project_id = ? AND edge_id = ? ORDER BY file_path LIMIT 33",
                        (project_id, edge["edge_id"]),
                    )]
                    edge_evidence = [dict(item) for item in connection.execute(
                        "SELECT file_path AS path, version_token, created_at FROM edge_evidence "
                        "WHERE project_id = ? AND edge_id = ? ORDER BY file_path LIMIT ? OFFSET ?",
                        (project_id, edge["edge_id"], effective_evidence_limit, evidence_offset),
                    )]
                    edge_freshness = (
                        self._check_freshness(
                            project_id, self.projects[project_id], freshness_secret, all_edge_evidence,
                            freshness_cache, freshness_budget,
                        ) if edge_evidence_total <= MAX_EVIDENCE_REFERENCES else {
                            "status": "unknown", "checked_files": 0, "total_files": edge_evidence_total,
                            "bytes_hashed": 0, "complete": False, "reason": "evidence_count_limit", "files": [],
                        }
                    ) if check_freshness else cached_freshness("edge_freshness", "edge_id", edge["edge_id"], edge_evidence_total)
                    edge_observed_at = utc_now() if check_freshness and not edge["managed"] else None
                    if edge_observed_at:
                        edge_freshness["observed_at"] = edge_observed_at
                    if check_freshness and not edge["managed"]:
                        connection.execute(
                            "INSERT INTO edge_freshness(project_id, edge_id, status, observed_at, stale_paths_json, "
                            "checked_files, total_files, bytes_hashed, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(project_id, edge_id) DO UPDATE SET status=excluded.status, "
                            "observed_at=excluded.observed_at, stale_paths_json=excluded.stale_paths_json, "
                            "checked_files=excluded.checked_files, total_files=excluded.total_files, "
                            "bytes_hashed=excluded.bytes_hashed, reason=excluded.reason",
                            (project_id, edge["edge_id"], edge_freshness["status"], edge_observed_at,
                             json.dumps([item["path"] for item in edge_freshness["files"] if item["status"] == "stale"], ensure_ascii=False),
                             edge_freshness["checked_files"], edge_freshness["total_files"],
                             edge_freshness["bytes_hashed"], edge_freshness["reason"]),
                        )
                    try:
                        edge_roles = json.loads(edge["roles_json"])
                    except (KeyError, TypeError, json.JSONDecodeError):
                        edge_roles = ["unspecified"]
                    if edge["relation"] == "maps_to" and not edge_roles:
                        edge_roles = ["unspecified"]
                    neighbors.append({
                        "edge_id": edge["edge_id"], "relation": edge["relation"], "direction": direction,
                        "source_id": edge["source_id"], "target_id": edge["target_id"],
                        "roles": edge_roles,
                        "peer": {"id": peer["node_id"], "type": peer["type"], "name": peer["name"],
                                 "summary": peer["summary"], "path": peer["path"],
                                 "created_at": peer["created_at"], "updated_at": peer["updated_at"]},
                        "evidence": edge_evidence,
                        "evidence_page": {
                            "offset": evidence_offset, "limit": effective_evidence_limit,
                            "total": edge_evidence_total,
                            "next_offset": evidence_offset + len(edge_evidence)
                            if evidence_offset + len(edge_evidence) < edge_evidence_total else None,
                            "complete": evidence_offset + len(edge_evidence) >= edge_evidence_total,
                        },
                        "freshness": edge_freshness,
                    })
                    if peer["type"] == "File" and peer["path"]:
                        info = connection.execute(
                            "SELECT size, mtime_ns, indexed_at FROM files WHERE project_id = ? AND path = ? AND type = 'file'",
                            (project_id, peer["path"]),
                        ).fetchone()
                        files[peer["path"]] = {
                            "node_id": peer["node_id"], "path": peer["path"], "type": "File",
                            "size": info["size"] if info else None,
                            "mtime_ns": info["mtime_ns"] if info else None,
                            "indexed_at": info["indexed_at"] if info else None,
                        }
                    for evidence in all_edge_evidence:
                        info = connection.execute(
                            "SELECT node_id, size, mtime_ns, indexed_at FROM files WHERE project_id = ? AND path = ? AND type = 'file'",
                            (project_id, evidence["path"]),
                        ).fetchone()
                        files[evidence["path"]] = {
                            "node_id": info["node_id"] if info else None,
                            "path": evidence["path"],
                            "type": "File" if info else "missing_file",
                            "size": info["size"] if info else None,
                            "mtime_ns": info["mtime_ns"] if info else None,
                            "indexed_at": info["indexed_at"] if info else None,
                        }
                def structure_peers(direction: str) -> tuple[list[dict[str, Any]], int]:
                    endpoint = "target_id" if direction == "parents" else "source_id"
                    peer_id = "source_id" if direction == "parents" else "target_id"
                    total = connection.execute(
                        f"SELECT COUNT(*) FROM map_edges WHERE project_id = ? AND relation = 'contains' AND {endpoint} = ?",
                        (project_id, node_id),
                    ).fetchone()[0]
                    rows = connection.execute(
                        f"SELECT n.node_id AS id, n.type, n.name, n.summary, n.path "
                        f"FROM map_edges e JOIN map_nodes n ON n.project_id=e.project_id AND n.node_id=e.{peer_id} "
                        f"WHERE e.project_id = ? AND e.relation = 'contains' AND e.{endpoint} = ? "
                        "ORDER BY n.name_fold, n.node_id LIMIT ?",
                        (project_id, node_id, effective_limit + 1),
                    ).fetchall()
                    return [dict(item) for item in rows[:effective_limit]], total

                parents, parent_total = structure_peers("parents")
                children, child_total = structure_peers("children")
                ancestors: list[dict[str, Any]] = []
                ancestor_parent_rows = connection.execute(
                    "SELECT source_id FROM map_edges WHERE project_id = ? AND relation = 'contains' "
                    "AND target_id = ? ORDER BY source_id LIMIT ?",
                    (project_id, node_id, MAX_TRAVERSE_NODES + 1),
                ).fetchall()
                pending: list[tuple[str, int]] = [
                    (parent["source_id"], 1) for parent in ancestor_parent_rows[:MAX_TRAVERSE_NODES]
                ]
                seen: set[str] = {node_id}
                ancestor_complete = parent_total <= MAX_TRAVERSE_NODES
                ancestor_reason = None if ancestor_complete else "visit_budget"
                while pending and len(ancestors) < MAX_CONTEXT_DEPTH:
                    current, depth = pending.pop(0)
                    if current in seen:
                        continue
                    seen.add(current)
                    current_row = connection.execute(
                        "SELECT node_id AS id, type, name, summary, path FROM map_nodes WHERE project_id = ? AND node_id = ?",
                        (project_id, current),
                    ).fetchone()
                    if current_row:
                        ancestors.append(dict(current_row))
                    if depth < MAX_CONTEXT_DEPTH:
                        parents_rows = connection.execute(
                            "SELECT n.node_id FROM map_nodes n JOIN map_edges e "
                            "ON e.project_id = n.project_id AND e.source_id = n.node_id "
                            "WHERE e.project_id = ? AND e.target_id = ? AND e.relation = 'contains' "
                            "ORDER BY n.name_fold, n.node_id LIMIT ?",
                            (project_id, current, MAX_TRAVERSE_NODES + 1),
                        ).fetchall()
                        if len(parents_rows) > MAX_TRAVERSE_NODES:
                            ancestor_complete = False
                            ancestor_reason = "visit_budget"
                        pending.extend(
                            (parent["node_id"], depth + 1)
                            for parent in parents_rows[:MAX_TRAVERSE_NODES]
                            if parent["node_id"] not in seen
                        )
                    else:
                        ancestor_complete = False
                        ancestor_reason = "depth_limit"
                if pending:
                    ancestor_complete = False
                    ancestor_reason = "result_limit"
                node_evidence_page = {
                    "offset": evidence_offset, "limit": effective_evidence_limit,
                    "total": node_evidence_total,
                    "next_offset": evidence_offset + len(node_evidence)
                    if evidence_offset + len(node_evidence) < node_evidence_total else None,
                    "complete": evidence_offset + len(node_evidence) >= node_evidence_total,
                }
                completeness = {
                    "neighbors": {"complete": neighbor_total <= len(neighbors), "total": neighbor_total,
                                  "returned": len(neighbors), "reason": None if neighbor_total <= len(neighbors) else "neighbor_limit",
                                  "continue_with": None if neighbor_total <= len(neighbors) else "traverse"},
                    "parents": {"complete": parent_total <= len(parents), "total": parent_total,
                                "returned": len(parents), "reason": None if parent_total <= len(parents) else "neighbor_limit",
                                "continue_with": None if parent_total <= len(parents) else "traverse"},
                    "children": {"complete": child_total <= len(children), "total": child_total,
                                 "returned": len(children), "reason": None if child_total <= len(children) else "neighbor_limit",
                                 "continue_with": None if child_total <= len(children) else "traverse"},
                    "ancestors": {"complete": ancestor_complete, "returned": len(ancestors),
                                  "reason": ancestor_reason,
                                  "continue_with": "traverse" if not ancestor_complete else None},
                    "node_evidence": node_evidence_page,
                    "files": {
                        "complete": node_evidence_page["complete"] and neighbor_total <= len(neighbors)
                        and all(item["evidence_page"]["complete"] for item in neighbors),
                        "returned": len(files),
                        "reason": None if node_evidence_page["complete"] and neighbor_total <= len(neighbors)
                        and all(item["evidence_page"]["complete"] for item in neighbors) else "source_section_incomplete",
                        "continue_with": None if node_evidence_page["complete"] and neighbor_total <= len(neighbors)
                        and all(item["evidence_page"]["complete"] for item in neighbors) else "context_paging",
                    },
                }
                response: dict[str, Any] = {
                    "ok": True, "project_id": project_id, "node": node,
                    "node_freshness": node_freshness,
                    "structure": {"parents": parents, "children": children, "ancestors": ancestors},
                    "files": sorted(files.values(), key=lambda item: (item["path"].casefold(), item["path"])),
                    "evidence": node_evidence,
                    "evidence_page": node_evidence_page,
                    "neighbors": neighbors,
                    "completeness": completeness,
                    "freshness_check": {
                        "performed": check_freshness,
                        "files_checked": len(freshness_cache),
                        "file_limit": MAX_FRESHNESS_FILES,
                        "bytes_hashed": freshness_budget["bytes"],
                        "byte_limit": MAX_FRESHNESS_BYTES,
                        "elapsed_ms": max(0, int((time.monotonic() - freshness_budget["started"]) * 1000))
                        if check_freshness else 0,
                        "time_limit_ms": int(MAX_FRESHNESS_SECONDS * 1000),
                    },
                    "truncated": any(not item["complete"] for item in completeness.values()),
                    "output_limited": False,
                }
                # Preserve node identity and per-section continuation cues while
                # shrinking optional arrays to the response budget.
                sections = {
                    "files": response["files"], "neighbors": response["neighbors"],
                    "node_evidence": response["evidence"],
                    "ancestors": response["structure"]["ancestors"],
                    "children": response["structure"]["children"],
                    "parents": response["structure"]["parents"],
                }
                original_lengths = {key: len(value) for key, value in sections.items()}
                removable_lists = list(sections.values())
                while _json_size(response) > MAX_MAP_OUTPUT_BYTES and any(removable_lists):
                    target_list = max((items for items in removable_lists if items), key=len)
                    target_list.pop()
                    response["output_limited"] = True
                    response["truncated"] = True
                if response["output_limited"]:
                    for section, values in sections.items():
                        if len(values) < original_lengths[section]:
                            completeness[section]["complete"] = False
                            completeness[section]["reason"] = "output_budget"
                            completeness[section]["returned"] = len(values)
                            completeness[section]["continue_with"] = (
                                "evidence_offset" if section == "node_evidence" else
                                "traverse" if section in {"neighbors", "parents", "children", "ancestors"} else None
                            )
                    response["evidence_page"]["next_offset"] = evidence_offset + len(response["evidence"])
                    response["evidence_page"]["complete"] = completeness["node_evidence"]["complete"]
                    response["truncated"] = any(not item["complete"] for item in completeness.values())
                return response
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    @staticmethod
    def _make_traverse_cursor(secret: bytes, payload: dict[str, Any]) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return encoded + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def resolve_paths(self, project_id: str, paths: list[str]) -> dict[str, Any]:
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        if type(paths) is not list or not paths or len(paths) > 100:
            return {"ok": False, "error": "invalid_input", "message": "paths 必须是 1–100 项的相对路径数组。", "project_id": project_id}
        project = self.projects[project_id]
        results = []
        try:
            with self.store._connection() as connection:
                for index, raw_path in enumerate(paths):
                    item: dict[str, Any] = {"index": index, "input_path": raw_path}
                    try:
                        normalized = project._normalise_relative(raw_path, allow_root=False)
                        canonical, disk_path = project._path_for(normalized)
                        info = disk_path.lstat()
                        is_directory = disk_path.is_dir()
                        if project._is_ignored(canonical, is_dir=is_directory):
                            raise ToolFailure("ignored_path", f"路径受项目忽略规则排除：{canonical}")
                    except ToolFailure as exc:
                        item.update({"ok": False, "error": exc.code, "message": exc.message})
                        results.append(item)
                        continue
                    row = connection.execute(
                        "SELECT type, node_id FROM files WHERE project_id = ? AND path = ?",
                        (project_id, canonical),
                    ).fetchone()
                    if not row:
                        item.update({
                            "ok": True, "path": canonical, "kind": "directory" if is_directory else "file",
                            "node_id": None, "indexed": False, "refresh_required": True,
                            "message": "该路径尚未进入文件清单；先刷新此目录后再建立关系。",
                        })
                    elif row["type"] != "file":
                        item.update({
                            "ok": False, "path": canonical, "kind": row["type"], "node_id": None,
                            "indexed": True, "error": "not_file", "message": "语义关系只能精确指向文件。",
                        })
                    else:
                        item.update({
                            "ok": True, "path": canonical, "kind": "file", "node_id": row["node_id"],
                            "indexed": True, "refresh_required": False,
                        })
                    results.append(item)
            return {
                "ok": True, "project_id": project_id, "results": results,
                "resolved_count": sum(item.get("ok") and item.get("indexed") for item in results),
                "refresh_required_count": sum(item.get("refresh_required", False) for item in results),
                "error_count": sum(not item.get("ok") for item in results),
            }
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    def coverage(self, project_id: str) -> dict[str, Any]:
        """Summarize explicit semantic associations without treating files as understood."""
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "project_id": project_id}
        try:
            with self.store._connection() as connection:
                file_count = connection.execute(
                    "SELECT COUNT(*) FROM files WHERE project_id = ? AND type = 'file'", (project_id,)
                ).fetchone()[0]
                mapped_count = connection.execute(
                    "SELECT COUNT(DISTINCT f.path) FROM map_edges e JOIN map_nodes f "
                    "ON f.project_id = e.project_id AND f.node_id = e.target_id AND f.type = 'File' "
                    "WHERE e.project_id = ? AND e.relation = 'maps_to'", (project_id,)
                ).fetchone()[0]
                evidence_paths = {
                    row["file_path"] for row in connection.execute(
                        "SELECT file_path FROM node_evidence WHERE project_id = ? "
                        "UNION SELECT file_path FROM edge_evidence WHERE project_id = ?",
                        (project_id, project_id),
                    )
                }
                mapped_paths = {row["path"] for row in connection.execute(
                    "SELECT f.path FROM map_edges e JOIN map_nodes f "
                    "ON f.project_id=e.project_id AND f.node_id=e.target_id AND f.type='File' "
                    "WHERE e.project_id=? AND e.relation='maps_to'", (project_id,)
                )}
                current_file_paths = {row["path"] for row in connection.execute(
                    "SELECT path FROM files WHERE project_id = ? AND type = 'file'", (project_id,)
                )}
                associated_paths = (mapped_paths | evidence_paths) & current_file_paths
                semantic_node_count = connection.execute(
                    "SELECT COUNT(*) FROM map_nodes WHERE project_id = ? AND type IN ('Module','Concept')",
                    (project_id,),
                ).fetchone()[0]
                semantic_edge_count = connection.execute(
                    "SELECT COUNT(*) FROM map_edges WHERE project_id = ? AND managed = 0", (project_id,)
                ).fetchone()[0]
                relation_counts = {relation: 0 for relation in RELATION_TYPES}
                relation_counts.update({
                    row["relation"]: row["count"]
                    for row in connection.execute(
                        "SELECT relation, COUNT(*) AS count FROM map_edges "
                        "WHERE project_id = ? AND managed = 0 GROUP BY relation",
                        (project_id,),
                    )
                })
                node_counts = {"Module": 0, "Concept": 0}
                node_counts.update({
                    row["type"]: row["count"]
                    for row in connection.execute(
                        "SELECT type, COUNT(*) AS count FROM map_nodes "
                        "WHERE project_id = ? AND type IN ('Module', 'Concept') GROUP BY type",
                        (project_id,),
                    )
                })
                freshness_counts = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM map_nodes WHERE project_id = ? AND type IN ('Module','Concept')) + "
                    "(SELECT COUNT(*) FROM map_edges WHERE project_id = ? AND managed = 0) AS owner_count, "
                    "(SELECT COUNT(*) FROM node_freshness WHERE project_id = ? AND status='stale') + "
                    "(SELECT COUNT(*) FROM edge_freshness WHERE project_id = ? AND status='stale') AS stale_count, "
                    "(SELECT COUNT(*) FROM node_freshness WHERE project_id = ? AND status='unknown') + "
                    "(SELECT COUNT(*) FROM edge_freshness WHERE project_id = ? AND status='unknown') AS unknown_count, "
                    "(SELECT COUNT(*) FROM node_freshness WHERE project_id = ? AND status='fresh') + "
                    "(SELECT COUNT(*) FROM edge_freshness WHERE project_id = ? AND status='fresh') AS fresh_count, "
                    "max((SELECT MAX(observed_at) FROM node_freshness WHERE project_id = ?), "
                    "(SELECT MAX(observed_at) FROM edge_freshness WHERE project_id = ?)) AS latest_observed_at",
                    (project_id, project_id, project_id, project_id, project_id, project_id, project_id, project_id, project_id, project_id),
                ).fetchone()
                unchecked_owners = max(0, freshness_counts["owner_count"] - freshness_counts["stale_count"]
                                       - freshness_counts["unknown_count"] - freshness_counts["fresh_count"])
                return {
                    "ok": True, "project_id": project_id,
                    "coverage": {
                        "file_manifest_count": file_count,
                        "mapped_file_count": mapped_count,
                        "evidence_file_count": len(evidence_paths),
                        "associated_file_count": len(associated_paths),
                        "unassociated_file_count": max(0, file_count - len(associated_paths)),
                        "semantic_node_count": semantic_node_count,
                        "semantic_edge_count": semantic_edge_count,
                        "node_counts": node_counts,
                        "relation_counts": relation_counts,
                        "freshness": {
                            "known_stale_owner_count": freshness_counts["stale_count"],
                            "last_observed_fresh_owner_count": freshness_counts["fresh_count"],
                            "last_observed_unknown_owner_count": freshness_counts["unknown_count"],
                            "unchecked_owner_count": unchecked_owners,
                            "latest_observation_at": freshness_counts["latest_observed_at"],
                            "state": "last_context_observation",
                            "note": "计数来自每个 owner 最近一次 context 检查，可能已过时；status 不会全库哈希，需用 context 复核当前状态。刷新不会清除已观察到的 stale。",
                        },
                        "scope": "仅统计 maps_to 与已存 evidence；文件关联范围不等于理解正确率。",
                    },
                }
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}

    @staticmethod
    def _read_traverse_cursor(secret: bytes, cursor: str) -> dict[str, Any] | None:
        try:
            encoded, signature = cursor.split(".", 1)
            expected = base64.urlsafe_b64encode(
                hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
            ).decode("ascii").rstrip("=")
            if not hmac.compare_digest(signature, expected):
                return None
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def traverse(
        self,
        project_id: str,
        start_node_id: str,
        *,
        target_node_id: str | None = None,
        relations: list[str] | None = None,
        direction: str = "outgoing",
        max_depth: int = 2,
        node_limit: int = MAX_TRAVERSE_PAGE_NODES,
        edge_limit: int = MAX_TRAVERSE_PAGE_EDGES,
        cursor: str | None = None,
        node_types: list[str] | None = None,
    ) -> dict[str, Any]:
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        if not isinstance(start_node_id, str) or not NODE_ID_RE.fullmatch(start_node_id):
            return {"ok": False, "error": "invalid_node_id", "message": "start_node_id 格式无效。", "project_id": project_id}
        if target_node_id is not None and (not isinstance(target_node_id, str) or not NODE_ID_RE.fullmatch(target_node_id)):
            return {"ok": False, "error": "invalid_node_id", "message": "target_node_id 格式无效。", "project_id": project_id}
        if relations is None:
            relations = ["contains"]
        if type(relations) is not list or not relations or any(
            not isinstance(item, str) or item not in TRAVERSE_RELATIONS for item in relations
        ):
            return {"ok": False, "error": "invalid_input", "message": "relations 必须是 contains、depends_on、maps_to、related_to 的非空数组。", "project_id": project_id}
        relations = sorted(set(relations))
        if node_types is not None:
            if type(node_types) is not list or not node_types or any(
                not isinstance(item, str) or item not in NODE_TYPES for item in node_types
            ):
                return {"ok": False, "error": "invalid_input", "message": "node_types 必须是 Project、Module、Concept、File 的非空数组。", "project_id": project_id}
            node_types = sorted(set(node_types))
        if not isinstance(direction, str) or direction not in {"outgoing", "incoming", "both"}:
            return {"ok": False, "error": "invalid_input", "message": "direction 必须是 outgoing、incoming 或 both。", "project_id": project_id}
        if type(max_depth) is not int or max_depth < 0 or max_depth > MAX_TRAVERSE_DEPTH:
            return {"ok": False, "error": "invalid_input", "message": f"max_depth 必须在 0–{MAX_TRAVERSE_DEPTH} 之间。", "project_id": project_id}
        if type(node_limit) is not int or node_limit < 1 or type(edge_limit) is not int or edge_limit < 1:
            return {"ok": False, "error": "invalid_input", "message": "node_limit 和 edge_limit 必须为正整数。", "project_id": project_id}
        effective_node_limit = min(node_limit, MAX_TRAVERSE_PAGE_NODES)
        effective_edge_limit = min(edge_limit, MAX_TRAVERSE_PAGE_EDGES)
        params = {
            "project_id": project_id, "start_node_id": start_node_id, "target_node_id": target_node_id,
            "relations": relations, "direction": direction, "max_depth": max_depth,
            "node_limit": effective_node_limit, "edge_limit": effective_edge_limit,
            "node_types": node_types,
        }
        try:
            secret = self.store.version_secret()
            with self.store._connection() as connection:
                connection.execute("BEGIN")
                revision_row = connection.execute(
                    "SELECT map_revision FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                if not revision_row:
                    return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
                revision = revision_row["map_revision"]
                node_offset = edge_offset = 0
                if cursor is not None:
                    token_data = self._read_traverse_cursor(secret, cursor) if isinstance(cursor, str) else None
                    if token_data is None:
                        return {"ok": False, "error": "invalid_cursor", "message": "遍历游标无效；请从起点重新查询。", "project_id": project_id}
                    for key, value in params.items():
                        if token_data.get(key) != value:
                            return {"ok": False, "error": "invalid_cursor", "message": "遍历游标与项目或查询条件不匹配。", "project_id": project_id}
                    if token_data.get("revision") != revision:
                        return {
                            "ok": False, "error": "stale_cursor", "message": "地图已变化，必须从起点重新遍历。",
                            "project_id": project_id, "restart_required": True,
                        }
                    node_offset = token_data.get("node_offset", 0)
                    edge_offset = token_data.get("edge_offset", 0)
                    if type(node_offset) is not int or node_offset < 0 or type(edge_offset) is not int or edge_offset < 0:
                        return {"ok": False, "error": "invalid_cursor", "message": "遍历游标位置无效。", "project_id": project_id}

                first = connection.execute(
                    "SELECT node_id, type, name, summary, path, state FROM map_nodes WHERE project_id = ? AND node_id = ?",
                    (project_id, start_node_id),
                ).fetchone()
                if not first:
                    return {"ok": False, "error": "node_not_found", "message": f"未找到起点节点：{start_node_id}", "project_id": project_id}
                if node_types is not None and first["type"] not in node_types:
                    return {"ok": False, "error": "invalid_input", "message": "起点节点类型必须包含在 node_types 中。", "project_id": project_id}
                if target_node_id and not connection.execute(
                    "SELECT 1 FROM map_nodes WHERE project_id = ? AND node_id = ?", (project_id, target_node_id)
                ).fetchone():
                    return {"ok": False, "error": "node_not_found", "message": f"未找到目标节点：{target_node_id}", "project_id": project_id}

                node_rows: dict[str, dict[str, Any]] = {start_node_id: dict(first)}
                depths = {start_node_id: 0}
                predecessor: dict[str, tuple[str, str]] = {}
                queue = deque([start_node_id])
                edge_rows: dict[str, dict[str, Any]] = {}
                stop_reason = None
                started = time.monotonic()
                depth_boundary = False
                while queue:
                    if time.monotonic() - started > MAX_TRAVERSE_SECONDS:
                        stop_reason = "time_budget"
                        break
                    current = queue.popleft()
                    current_depth = depths[current]
                    if current_depth >= max_depth:
                        depth_boundary = True
                        continue
                    clauses: list[str] = []
                    sql_params: list[Any] = [project_id]
                    for relation in relations:
                        if direction == "both" or relation == "related_to":
                            clauses.append("(relation = ? AND (source_id = ? OR target_id = ?))")
                            sql_params.extend((relation, current, current))
                        elif direction == "outgoing":
                            clauses.append("(relation = ? AND source_id = ?)")
                            sql_params.extend((relation, current))
                        else:
                            clauses.append("(relation = ? AND target_id = ?)")
                            sql_params.extend((relation, current))
                    peer_type_clause = ""
                    if node_types is not None:
                        placeholders = ", ".join("?" for _ in node_types)
                        peer_type_clause = (
                            " AND (CASE WHEN source_id = ? THEN target_id ELSE source_id END) IN "
                            f"(SELECT node_id FROM map_nodes WHERE project_id = ? AND type IN ({placeholders}))"
                        )
                        sql_params.extend((current, project_id, *node_types))
                    rows = connection.execute(
                        "SELECT edge_id, source_id, relation, target_id, roles_json FROM map_edges WHERE project_id = ? AND (" +
                        " OR ".join(clauses) + ")" + peer_type_clause +
                        " ORDER BY relation, source_id, target_id LIMIT ?",
                        [*sql_params, MAX_TRAVERSE_EDGES - len(edge_rows) + 1],
                    ).fetchall()
                    for edge in rows:
                        if edge["edge_id"] not in edge_rows:
                            if len(edge_rows) >= MAX_TRAVERSE_EDGES:
                                stop_reason = "edge_budget"
                                break
                            traversal_direction = (
                                "undirected" if edge["relation"] == "related_to" else
                                "outgoing" if edge["source_id"] == current else "incoming"
                            )
                            try:
                                edge_roles = json.loads(edge["roles_json"])
                            except (KeyError, TypeError, json.JSONDecodeError):
                                edge_roles = ["unspecified"] if edge["relation"] == "maps_to" else []
                            if edge["relation"] == "maps_to" and not edge_roles:
                                edge_roles = ["unspecified"]
                            edge_rows[edge["edge_id"]] = {
                                "edge_id": edge["edge_id"], "relation": edge["relation"],
                                "source_id": edge["source_id"], "target_id": edge["target_id"],
                                "traversal_direction": traversal_direction,
                                "roles": edge_roles,
                            }
                        peer_id = edge["target_id"] if edge["source_id"] == current else edge["source_id"]
                        if peer_id not in node_rows:
                            if len(node_rows) >= MAX_TRAVERSE_NODES:
                                stop_reason = "node_budget"
                                break
                            peer = connection.execute(
                                "SELECT node_id, type, name, summary, path, state FROM map_nodes "
                                "WHERE project_id = ? AND node_id = ?", (project_id, peer_id)
                            ).fetchone()
                            if peer is None:
                                continue
                            node_rows[peer_id] = dict(peer)
                            depths[peer_id] = current_depth + 1
                            predecessor[peer_id] = (current, edge["edge_id"])
                            queue.append(peer_id)
                    if stop_reason:
                        break

                complete = stop_reason is None and not depth_boundary
                if stop_reason is None and depth_boundary:
                    stop_reason = "depth_limit"
                nodes = list(node_rows.values())
                edges = list(edge_rows.values())
                shortest_path = None
                if target_node_id in node_rows:
                    path_ids: list[str] = []
                    current = target_node_id
                    while current != start_node_id:
                        prior, edge_id = predecessor[current]
                        path_ids.append(edge_id)
                        current = prior
                    path_ids.reverse()
                    shortest_path = [edge_rows[edge_id] for edge_id in path_ids]

                page_nodes = nodes[node_offset:node_offset + effective_node_limit]
                page_edges = edges[edge_offset:edge_offset + effective_edge_limit]
                base: dict[str, Any] = {
                    "ok": True, **params, "revision": revision,
                    "complete": complete, "stop_reason": stop_reason,
                    "scope": f"{len(relations)} relation(s), at most {max_depth} edge(s) from start",
                    "total_nodes": len(nodes), "total_edges": len(edges),
                    "node_offset": node_offset, "edge_offset": edge_offset,
                    "nodes": page_nodes, "edges": page_edges,
                    "target_reached": target_node_id in node_rows if target_node_id else None,
                    "shortest_path": shortest_path if target_node_id else None,
                    "path_selection": "first shortest path in stable relation/source/target order" if target_node_id else None,
                    "visited_nodes": len(node_rows), "visited_edges": len(edge_rows),
                    "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "output_limited": False,
                }
                next_cursor = None
                if node_offset + len(page_nodes) < len(nodes) or edge_offset + len(page_edges) < len(edges):
                    next_cursor = self._make_traverse_cursor(secret, {
                        **params, "revision": revision,
                        "node_offset": node_offset + len(page_nodes),
                        "edge_offset": edge_offset + len(page_edges),
                    })
                base["next_cursor"] = next_cursor
                base["truncated"] = next_cursor is not None or not complete
                while _json_size(base) > MAX_MAP_OUTPUT_BYTES and (base["nodes"] or base["edges"]):
                    if len(base["edges"]) >= len(base["nodes"]) and base["edges"]:
                        base["edges"].pop()
                    elif base["nodes"]:
                        base["nodes"].pop()
                    base["output_limited"] = True
                    next_node_offset = base["node_offset"] + len(base["nodes"])
                    next_edge_offset = base["edge_offset"] + len(base["edges"])
                    base["next_cursor"] = None
                    if next_node_offset < len(nodes) or next_edge_offset < len(edges):
                        base["next_cursor"] = self._make_traverse_cursor(secret, {
                            **params, "revision": revision,
                            "node_offset": next_node_offset, "edge_offset": next_edge_offset,
                        })
                base["truncated"] = base["next_cursor"] is not None or not complete
                return base
        except (StoreError, sqlite3.Error) as exc:
            return {"ok": False, "error": "map_store_error", "message": str(exc), "project_id": project_id}


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
