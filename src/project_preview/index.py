from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from project_preview.filesystem import MAX_PREVIEW_OUTPUT_BYTES, ProjectFiles, ToolFailure
from project_preview.store import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    IndexStore,
    StoreError,
)
from project_preview.semantic_map import SemanticMap
from project_preview.versions import MAX_VERSION_HASH_SECONDS


MAX_STATUS_LIMIT = 50
MAX_STATUS_OUTPUT_BYTES = 48_000
MAX_STATUS_OFFSET = (1 << 63) - 1
_REFRESH_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


def _refresh_lock(store: IndexStore, project_id: str) -> threading.Lock:
    key = (os.path.normcase(str(store.path)), project_id)
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


class ProjectIndex:
    """Coordinates safe filesystem scans with atomic persistent manifest updates."""

    def __init__(self, projects: dict[str, ProjectFiles], store: IndexStore) -> None:
        self.projects = dict(projects)
        self.store = store
        self.semantic_map = SemanticMap(store, self.projects)
        self._refresh_locks = {
            project_id: _refresh_lock(store, project_id) for project_id in projects
        }

    def project_ids(self) -> list[str]:
        return sorted(self.projects)

    def _project(self, project_id: str) -> ProjectFiles | None:
        return self.projects.get(project_id)

    def refresh(self, project_id: str, directory: str = "") -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        try:
            requested_scope = project._normalise_relative(directory, allow_root=True)
        except ToolFailure as exc:
            return project._failure(exc)
        with self._refresh_locks[project_id]:
            try:
                refresh_id = self.store.start_refresh(project_id, requested_scope)
            except StoreError as exc:
                return {"ok": False, "error": "index_store_error", "message": str(exc)}

            scan_started = time.perf_counter()
            try:
                scope, entries = project.scan_manifest(requested_scope)
                scan_duration_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                counts = self.store.complete_refresh(
                    refresh_id,
                    project_id,
                    scope,
                    entries,
                    scan_duration_ms=scan_duration_ms,
                )
                elapsed_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                return {
                    "ok": True,
                    "project_id": project_id,
                    "scope": scope or ".",
                    "status": "succeeded",
                    "refresh_id": refresh_id,
                    "discovered_file_count": counts["files"],
                    "discovered_directory_count": counts["directories"],
                    "removed_entry_count": counts["removed"],
                    "indexed_bytes": counts["indexed_bytes"],
                    "scan_duration_ms": counts["scan_duration_ms"],
                    "elapsed_ms": elapsed_ms,
                }
            except ToolFailure as exc:
                scan_duration_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                try:
                    self.store.fail_refresh(
                        refresh_id, exc.code, exc.message, scan_duration_ms=scan_duration_ms
                    )
                except StoreError:
                    pass
                elapsed_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                result = project._failure(exc)
                result.update({
                    "project_id": project_id,
                    "scope": requested_scope or ".",
                    "status": "failed",
                    "refresh_id": refresh_id,
                    "scan_duration_ms": scan_duration_ms,
                    "elapsed_ms": elapsed_ms,
                })
                return result
            except StoreError as exc:
                scan_duration_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                try:
                    self.store.fail_refresh(
                        refresh_id,
                        "index_store_error",
                        str(exc),
                        scan_duration_ms=scan_duration_ms,
                    )
                except StoreError:
                    pass
                elapsed_ms = max(0, int((time.perf_counter() - scan_started) * 1_000))
                return {
                    "ok": False,
                    "error": "index_store_error",
                    "message": str(exc),
                    "project_id": project_id,
                    "scope": requested_scope or ".",
                    "status": "failed",
                    "refresh_id": refresh_id,
                    "scan_duration_ms": scan_duration_ms,
                    "elapsed_ms": elapsed_ms,
                }

    def refresh_history(
        self, project_id: str, *, limit: int = 20, offset: int = 0
    ) -> dict[str, object]:
        if project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        return self.store.refresh_history(project_id, limit=limit, offset=offset)

    def preview(
        self,
        project_id: str,
        path: str,
        *,
        start_line: int = 1,
        line_count: int = 80,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        try:
            secret = self.store.version_secret()
        except StoreError:
            secret = None
        snapshot_result = (
            project.capture_versioned_snapshot(
                path, project_id, secret, max_seconds=MAX_VERSION_HASH_SECONDS
            )
            if secret else None
        )
        snapshot = snapshot_result.content if snapshot_result else None
        result = project.preview(
            path=path,
            start_line=start_line,
            line_count=line_count,
            _snapshot=snapshot,
        )
        if not result.get("ok"):
            return result
        version = snapshot_result.version if snapshot_result else None
        if version and version.token:
            result["version_token"] = version.token
            result["version_token_status"] = "available"
            result["version_token_reason"] = None
            result["source_mtime_ns"] = version.mtime_ns
            result["source_size"] = version.size
        else:
            result["version_token"] = None
            result["version_token_status"] = "unavailable"
            result["version_token_reason"] = version.reason if version and version.reason else "unavailable"
            result["source_mtime_ns"] = None
            result["source_size"] = None
        lines = result.get("lines", [])
        while lines and project._json_size(result) > MAX_PREVIEW_OUTPUT_BYTES:
            lines.pop()
            next_line = lines[-1]["line_number"] + 1 if lines else start_line
            result.update(
                truncated=True,
                reason="output_budget",
                next_start_line=next_line,
                continuation=f"预览输出达到 {MAX_PREVIEW_OUTPUT_BYTES} 字节预算；再次调用 preview(path={path!r}, start_line={next_line}, line_count={line_count})。",
            )
        return result

    def preview_tail(self, project_id: str, path: str, *, line_count: int = 80) -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        return {**project.preview_tail(path=path, line_count=line_count), "project_id": project_id}

    def update_map(self, project_id: str, **changes: Any) -> dict[str, Any]:
        return self.semantic_map.update_map(project_id, **changes)

    def search_map(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        case_sensitive: bool = True,
        node_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.semantic_map.search(
            project_id, query, limit=limit, offset=offset, case_sensitive=case_sensitive,
            node_types=node_types,
        )

    def search_file(
        self, project_id: str, path: str, query: str, *, limit: int = 20,
        context_lines: int = 2, case_sensitive: bool = False,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        result = project.search(
            mode="source", query=query, file_path=path, limit=limit,
            context_lines=context_lines, case_sensitive=case_sensitive,
        )
        return {**result, "project_id": project_id, "file_path": path}

    def context(
        self, project_id: str, node_id: str, *, neighbor_limit: int = 10,
        evidence_offset: int = 0, evidence_limit: int = 20, check_freshness: bool = True,
    ) -> dict[str, Any]:
        return self.semantic_map.context(
            project_id, node_id, neighbor_limit=neighbor_limit,
            evidence_offset=evidence_offset, evidence_limit=evidence_limit,
            check_freshness=check_freshness,
        )

    def verify_freshness(self, project_id: str, owner_type: str, owner_id: str) -> dict[str, Any]:
        return self.semantic_map.verify_freshness(project_id, owner_type, owner_id)

    def review_changes(self, project_id: str, **options: Any) -> dict[str, Any]:
        return self.semantic_map.review_changes(project_id, **options)

    def traverse(self, project_id: str, start_node_id: str, **options: Any) -> dict[str, Any]:
        return self.semantic_map.traverse(project_id, start_node_id, **options)

    def resolve_paths(self, project_id: str, paths: list[str]) -> dict[str, Any]:
        return self.semantic_map.resolve_paths(project_id, paths)

    def status(
        self,
        project_id: str | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if project_id is not None and project_id not in self.projects:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        if type(limit) is not int or limit < 1:
            return {"ok": False, "error": "invalid_input", "message": "limit 必须是正整数。"}
        if type(offset) is not int or offset < 0 or offset > MAX_STATUS_OFFSET:
            return {"ok": False, "error": "invalid_input", "message": "offset 必须是非负整数。"}
        effective_limit = min(limit, MAX_STATUS_LIMIT)
        project_ids = [project_id] if project_id is not None else self.project_ids()
        total = len(project_ids)
        page_ids = project_ids[offset : offset + effective_limit]
        try:
            result = self.store.status(project_ids=page_ids)
            entries = result["projects"]
            for entry in entries:
                coverage = self.semantic_map.coverage(entry["project_id"])
                if coverage.get("ok"):
                    entry["semantic_map"] = coverage["coverage"]
            response: dict[str, Any] = {
                "ok": True,
                "configured_project_count": len(self.projects),
                "limit": effective_limit,
                "offset": offset,
                "total": total,
                "projects": entries,
                "next_offset": None,
                "output_limited": False,
                "reason": None,
            }
            limited = False
            while entries and _json_size(response) > MAX_STATUS_OUTPUT_BYTES:
                entries.pop()
                limited = True
            response["next_offset"] = offset + len(entries) if offset + len(entries) < total else None
            response["output_limited"] = limited
            response["reason"] = "output_budget" if limited else None
            return response
        except StoreError as exc:
            return {"ok": False, "error": "index_store_error", "message": str(exc)}

    def list_files(
        self,
        project_id: str,
        directory: str = "",
        *,
        include_directories: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
        try:
            scope = project._normalise_relative(directory, allow_root=True)
        except ToolFailure as exc:
            return project._failure(exc)
        if type(limit) is int and limit > MAX_LIST_LIMIT:
            limit = MAX_LIST_LIMIT
        result = self.store.list_files(
            project_id,
            scope,
            include_directories=include_directories,
            limit=limit,
            offset=offset,
        )
        return result


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
