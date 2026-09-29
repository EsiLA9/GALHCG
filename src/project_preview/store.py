from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from project_preview.map_ids import file_node_id, managed_edge_id, project_node_id


SCHEMA_VERSION = 7
MAX_PROJECT_ID_LENGTH = 64
MAX_ERROR_MESSAGE_LENGTH = 2_000
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
MAX_LIST_OUTPUT_BYTES = 48_000
MAX_SQLITE_OFFSET = (1 << 63) - 1
MAX_REFRESH_HISTORY_LIMIT = 100
MAX_REFRESH_HISTORY_OUTPUT_BYTES = 48_000


class StoreError(ValueError):
    """Persistent project index configuration or storage error."""


@dataclass(frozen=True)
class IndexedEntry:
    path: str
    kind: str
    size: int | None
    mtime_ns: int | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id:
        raise StoreError("project_id 不能为空。")
    if len(project_id) > MAX_PROJECT_ID_LENGTH:
        raise StoreError(f"project_id 最多 {MAX_PROJECT_ID_LENGTH} 个字符。")
    if not project_id[0].isascii() or not project_id[0].isalnum():
        raise StoreError("project_id 必须以 ASCII 字母或数字开头。")
    if any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in project_id):
        raise StoreError("project_id 只能包含 ASCII 字母、数字、点、下划线和连字符。")
    if project_id in {".", ".."}:
        raise StoreError("project_id 不能是路径片段。")
    return project_id


class IndexStore:
    """Versioned SQLite store. It stores metadata only, never source contents."""

    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise StoreError("--data-dir 必须是绝对路径；服务不会依赖当前工作目录。")
        try:
            self.data_dir = data_dir.resolve(strict=False)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not self.data_dir.is_dir():
                raise StoreError("数据目录不是目录。")
        except OSError as exc:
            raise StoreError(f"无法创建服务数据目录：{exc}") from exc
        self.path = self.data_dir / "project-preview.sqlite3"
        self._initialize()

    @staticmethod
    def default_data_dir() -> Path:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA")
            if base:
                return Path(base) / "project-preview-mcp"
            return Path.home() / "AppData" / "Local" / "project-preview-mcp"
        if os.environ.get("XDG_DATA_HOME"):
            return Path(os.environ["XDG_DATA_HOME"]) / "project-preview-mcp"
        if sys_platform_is_macos():
            return Path.home() / "Library" / "Application Support" / "project-preview-mcp"
        return Path.home() / ".local" / "share" / "project-preview-mcp"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        with self._connection() as connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "schema_version INTEGER NOT NULL)"
                )
                row = connection.execute(
                    "SELECT schema_version FROM schema_meta WHERE singleton = 1"
                ).fetchone()
                version = int(row[0]) if row else 0
                if version > SCHEMA_VERSION:
                    raise StoreError(
                        f"索引数据库版本为 {version}，当前服务只支持到 {SCHEMA_VERSION}。"
                    )
                if version < 1:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE projects ("
                        "project_id TEXT PRIMARY KEY, "
                        "root TEXT NOT NULL, "
                        "root_key TEXT NOT NULL, "
                        "created_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE files ("
                        "project_id TEXT NOT NULL, "
                        "path TEXT NOT NULL, "
                        "type TEXT NOT NULL CHECK (type IN ('file', 'directory')), "
                        "size INTEGER, "
                        "mtime_ns INTEGER, "
                        "indexed_at TEXT NOT NULL, "
                        "PRIMARY KEY (project_id, path), "
                        "FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE)"
                    )
                    connection.execute(
                        "CREATE TABLE refresh_runs ("
                        "refresh_id TEXT PRIMARY KEY, "
                        "project_id TEXT NOT NULL, "
                        "scope TEXT NOT NULL, "
                        "status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')), "
                        "started_at TEXT NOT NULL, "
                        "finished_at TEXT, "
                        "file_count INTEGER, "
                        "directory_count INTEGER, "
                        "error_code TEXT, "
                        "error_message TEXT, "
                        "FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE)"
                    )
                    connection.execute(
                        "CREATE INDEX refresh_runs_project_started "
                        "ON refresh_runs(project_id, started_at DESC, refresh_id DESC)"
                    )
                    connection.execute(
                        "INSERT INTO schema_meta(singleton, schema_version) VALUES (1, 1) "
                        "ON CONFLICT(singleton) DO UPDATE SET schema_version = excluded.schema_version"
                    )
                    connection.commit()
                    version = 1
                if version < 2:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v2(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 2 WHERE singleton = 1"
                    )
                    connection.commit()
                    version = 2
                if version < 3:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v3(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 3 WHERE singleton = 1"
                    )
                    connection.commit()
                    version = 3
                if version < 4:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v4(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 4 WHERE singleton = 1"
                    )
                    connection.commit()
                    version = 4
                if version < 5:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v5(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 5 WHERE singleton = 1"
                    )
                    connection.commit()
                    version = 5
                if version < 6:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v6(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 6 WHERE singleton = 1"
                    )
                    connection.commit()
                    version = 6
                if version < 7:
                    connection.execute("BEGIN IMMEDIATE")
                    self._migrate_to_v7(connection)
                    connection.execute(
                        "UPDATE schema_meta SET schema_version = 7 WHERE singleton = 1"
                    )
                    connection.commit()
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError(f"初始化 SQLite 索引失败：{exc}") from exc

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE files ADD COLUMN node_id TEXT")
        connection.execute(
            "CREATE TABLE map_nodes ("
            "node_id TEXT NOT NULL, project_id TEXT NOT NULL, "
            "type TEXT NOT NULL CHECK (type IN ('Project', 'Module', 'Concept', 'File')), "
            "path TEXT, name TEXT NOT NULL, summary TEXT NOT NULL, aliases_json TEXT NOT NULL, "
            "name_fold TEXT NOT NULL, summary_fold TEXT NOT NULL, "
            "state TEXT CHECK (state IS NULL OR state IN ('tentative', 'confirmed')), "
            "managed INTEGER NOT NULL CHECK (managed IN (0, 1)), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "CHECK ((type IN ('Project', 'File') AND state IS NULL AND managed = 1) OR "
            "(type IN ('Module', 'Concept') AND state IN ('tentative', 'confirmed') AND managed = 0)), "
            "CHECK ((type = 'File' AND path IS NOT NULL) OR (type <> 'File' AND path IS NULL)), "
            "FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE, "
            "PRIMARY KEY (project_id, node_id))"
        )
        connection.execute(
            "CREATE UNIQUE INDEX map_nodes_file_path ON map_nodes(project_id, path) WHERE type = 'File'"
        )
        connection.execute(
            "CREATE INDEX map_nodes_project_name ON map_nodes(project_id, name_fold, node_id)"
        )
        connection.execute(
            "CREATE TABLE map_aliases ("
            "node_id TEXT NOT NULL, project_id TEXT NOT NULL, alias TEXT NOT NULL, alias_fold TEXT NOT NULL, "
            "PRIMARY KEY (project_id, node_id, alias), "
            "FOREIGN KEY (project_id, node_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE INDEX map_aliases_project_fold ON map_aliases(project_id, alias_fold, node_id)"
        )
        connection.execute(
            "CREATE TABLE map_edges ("
            "edge_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
            "source_id TEXT NOT NULL, relation TEXT NOT NULL CHECK (relation IN "
            "('contains', 'maps_to', 'depends_on', 'related_to')), target_id TEXT NOT NULL, "
            "managed INTEGER NOT NULL CHECK (managed IN (0, 1)), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "CHECK (source_id <> target_id), "
            "FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE, "
            "FOREIGN KEY (project_id, source_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE, "
            "FOREIGN KEY (project_id, target_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE, "
            "UNIQUE (project_id, relation, source_id, target_id), UNIQUE (edge_id, project_id))"
        )
        connection.execute(
            "CREATE INDEX map_edges_source ON map_edges(project_id, source_id, relation)"
        )
        connection.execute(
            "CREATE INDEX map_edges_target ON map_edges(project_id, target_id, relation)"
        )
        connection.execute(
            "CREATE TABLE node_evidence ("
            "node_id TEXT NOT NULL, project_id TEXT NOT NULL, "
            "file_path TEXT NOT NULL, version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (project_id, node_id, file_path), "
            "FOREIGN KEY (project_id, node_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE TABLE edge_evidence ("
            "edge_id TEXT NOT NULL, project_id TEXT NOT NULL, "
            "file_path TEXT NOT NULL, version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (project_id, edge_id, file_path), "
            "FOREIGN KEY (edge_id, project_id) REFERENCES map_edges(edge_id, project_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE TABLE map_metadata (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "version_secret BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO map_metadata(singleton, version_secret) VALUES (1, ?)", (os.urandom(32),)
        )

        for project in connection.execute("SELECT project_id, root FROM projects").fetchall():
            IndexStore._ensure_project_node(connection, project["project_id"], project["root"])
        for entry in connection.execute(
            "SELECT project_id, path FROM files WHERE type = 'file' ORDER BY project_id, path"
        ).fetchall():
            IndexStore._ensure_file_node(connection, entry["project_id"], entry["path"])

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        """Normalize legacy v2 evidence tables to the path-based schema.

        Early stage-4 builds used ``file_node_id`` columns even though they
        were also marked schema version 2.  The current API stores evidence by
        project-relative path so deleted files remain identifiable.  Detect
        that older v2 layout and migrate it transactionally without dropping
        evidence.
        """
        migrations = (
            ("node_evidence", "node_id", "node_id"),
            ("edge_evidence", "edge_id", "edge_id"),
        )
        for table, owner_column, owner_key in migrations:
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "file_path" in columns:
                continue
            if "file_node_id" not in columns:
                raise StoreError(f"无法识别 {table} 的证据表结构，数据库迁移已回滚。")

            legacy_table = f"{table}_legacy_v2"
            connection.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")
            if table == "node_evidence":
                connection.execute(
                    "CREATE TABLE node_evidence ("
                    "node_id TEXT NOT NULL, project_id TEXT NOT NULL, "
                    "file_path TEXT NOT NULL, version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "PRIMARY KEY (project_id, node_id, file_path), "
                    "FOREIGN KEY (project_id, node_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE)"
                )
            else:
                connection.execute(
                    "CREATE TABLE edge_evidence ("
                    "edge_id TEXT NOT NULL, project_id TEXT NOT NULL, "
                    "file_path TEXT NOT NULL, version_token TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "PRIMARY KEY (project_id, edge_id, file_path), "
                    "FOREIGN KEY (edge_id, project_id) REFERENCES map_edges(edge_id, project_id) ON DELETE CASCADE)"
                )

            join_sql = (
                f"SELECT old.{owner_column}, old.project_id, file.path, old.version_token, old.created_at "
                f"FROM {legacy_table} AS old "
                "JOIN map_nodes AS file ON file.project_id = old.project_id "
                "AND file.node_id = old.file_node_id AND file.type = 'File' AND file.path IS NOT NULL"
            )
            old_count = connection.execute(f"SELECT COUNT(*) FROM {legacy_table}").fetchone()[0]
            mapped_count = connection.execute(
                f"SELECT COUNT(*) FROM ({join_sql}) AS mapped"
            ).fetchone()[0]
            if old_count != mapped_count:
                raise StoreError(
                    f"{table} 中存在无法关联到项目文件的证据；数据库迁移已回滚。"
                )
            connection.execute(
                f"INSERT INTO {table} ({owner_key}, project_id, file_path, version_token, created_at) "
                + join_sql
            )
            connection.execute(f"DROP TABLE {legacy_table}")

    @staticmethod
    def _migrate_to_v4(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(refresh_runs)")}
        additions = (
            ("scan_duration_ms", "INTEGER"),
            ("indexed_bytes", "INTEGER"),
            ("removed_entry_count", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, definition in additions:
            if name not in columns:
                connection.execute(f"ALTER TABLE refresh_runs ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_to_v5(connection: sqlite3.Connection) -> None:
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
        if "map_revision" not in project_columns:
            connection.execute(
                "ALTER TABLE projects ADD COLUMN map_revision INTEGER NOT NULL DEFAULT 0"
            )
        edge_columns = {row["name"] for row in connection.execute("PRAGMA table_info(map_edges)")}
        if "roles_json" not in edge_columns:
            connection.execute(
                "ALTER TABLE map_edges ADD COLUMN roles_json TEXT NOT NULL DEFAULT '[\"unspecified\"]'"
            )
        # A revision token invalidates traversal cursors after any node or edge
        # change, including managed File nodes maintained by refresh.
        for table, action, alias in (
            ("map_nodes", "INSERT", "NEW"),
            ("map_nodes", "UPDATE", "NEW"),
            ("map_nodes", "DELETE", "OLD"),
            ("map_edges", "INSERT", "NEW"),
            ("map_edges", "UPDATE", "NEW"),
            ("map_edges", "DELETE", "OLD"),
        ):
            operation = action.lower()
            trigger = f"{table}_revision_{operation}"
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {action} ON {table} "
                f"BEGIN UPDATE projects SET map_revision = map_revision + 1 "
                f"WHERE project_id = {alias}.project_id; END"
            )

    @staticmethod
    def _migrate_to_v6(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS node_freshness ("
            "project_id TEXT NOT NULL, node_id TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK(status IN ('fresh','stale','unknown')), "
            "observed_at TEXT NOT NULL, stale_paths_json TEXT NOT NULL, "
            "checked_files INTEGER NOT NULL, total_files INTEGER NOT NULL, "
            "bytes_hashed INTEGER NOT NULL, reason TEXT, "
            "PRIMARY KEY(project_id, node_id), "
            "FOREIGN KEY(project_id, node_id) REFERENCES map_nodes(project_id, node_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS edge_freshness ("
            "project_id TEXT NOT NULL, edge_id TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK(status IN ('fresh','stale','unknown')), "
            "observed_at TEXT NOT NULL, stale_paths_json TEXT NOT NULL, "
            "checked_files INTEGER NOT NULL, total_files INTEGER NOT NULL, "
            "bytes_hashed INTEGER NOT NULL, reason TEXT, "
            "PRIMARY KEY(project_id, edge_id), "
            "FOREIGN KEY(edge_id, project_id) REFERENCES map_edges(edge_id, project_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS node_freshness_project_status ON node_freshness(project_id, status, observed_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS edge_freshness_project_status ON edge_freshness(project_id, status, observed_at)"
        )

    @staticmethod
    def _migrate_to_v7(connection: sqlite3.Connection) -> None:
        """Store the file metadata captured alongside each semantic evidence token."""
        for table in ("node_evidence", "edge_evidence"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "captured_mtime_ns" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN captured_mtime_ns INTEGER")
            if "captured_size" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN captured_size INTEGER")
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_file_path ON {table}(project_id, file_path)"
            )

    @staticmethod
    def _insert_auto_edge(
        connection: sqlite3.Connection, project_id: str, source_id: str, relation: str, target_id: str
    ) -> None:
        now = utc_now()
        connection.execute(
            "INSERT INTO map_edges(edge_id, project_id, source_id, relation, target_id, managed, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT(project_id, relation, source_id, target_id) "
            "DO UPDATE SET managed = 1",
            (managed_edge_id(project_id, relation, source_id, target_id), project_id, source_id, relation, target_id, now, now),
        )

    @staticmethod
    def _ensure_project_node(connection: sqlite3.Connection, project_id: str, root: str) -> None:
        node_id = project_node_id(project_id)
        name = Path(root).name or project_id
        now = utc_now()
        connection.execute(
            "INSERT INTO map_nodes(node_id, project_id, type, path, name, summary, aliases_json, name_fold, "
            "summary_fold, state, managed, created_at, updated_at) VALUES (?, ?, 'Project', NULL, ?, '', '[]', ?, '', NULL, 1, ?, ?) "
            "ON CONFLICT(project_id, node_id) DO UPDATE SET name = excluded.name, name_fold = excluded.name_fold, updated_at = excluded.updated_at",
            (node_id, project_id, name, name.casefold(), now, now),
        )

    @staticmethod
    def _ensure_file_node(connection: sqlite3.Connection, project_id: str, path: str) -> str:
        node_id = file_node_id(project_id, path)
        name = PurePosixPath(path).name
        now = utc_now()
        connection.execute(
            "INSERT INTO map_nodes(node_id, project_id, type, path, name, summary, aliases_json, name_fold, "
            "summary_fold, state, managed, created_at, updated_at) VALUES (?, ?, 'File', ?, ?, '', '[]', ?, '', NULL, 1, ?, ?) "
            "ON CONFLICT(project_id, node_id) DO UPDATE SET path = excluded.path, name = excluded.name, "
            "name_fold = excluded.name_fold, updated_at = excluded.updated_at",
            (node_id, project_id, path, name, name.casefold(), now, now),
        )
        connection.execute(
            "UPDATE files SET node_id = ? WHERE project_id = ? AND path = ?",
            (node_id, project_id, path),
        )
        IndexStore._insert_auto_edge(
            connection, project_id, project_node_id(project_id), "contains", node_id
        )
        return node_id

    def version_secret(self) -> bytes:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT version_secret FROM map_metadata WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT OR IGNORE INTO map_metadata(singleton, version_secret) VALUES (1, ?)",
                        (os.urandom(32),),
                    )
                    row = connection.execute(
                        "SELECT version_secret FROM map_metadata WHERE singleton = 1"
                    ).fetchone()
                return bytes(row[0])
        except sqlite3.Error as exc:
            raise StoreError(f"读取版本凭据密钥失败：{exc}") from exc

    @staticmethod
    def _root_key(root: Path) -> str:
        return os.path.normcase(os.path.normpath(str(root)))

    def register_project(self, project_id: str, root: Path) -> None:
        project_id = validate_project_id(project_id)
        resolved_root = root.resolve(strict=True)
        root_text = str(resolved_root)
        root_key = self._root_key(resolved_root)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT root, root_key FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                if current:
                    if current["root_key"] != root_key:
                        raise StoreError(
                            f"project_id '{project_id}' 已绑定到其他项目根目录（{current['root']}）。"
                            "同一 ID 不能切换工作目录；请为另一个工作目录分配新 ID。"
                        )
                else:
                    connection.execute(
                        "INSERT INTO projects(project_id, root, root_key, created_at) VALUES (?, ?, ?, ?)",
                        (project_id, root_text, root_key, utc_now()),
                    )
                self._ensure_project_node(connection, project_id, root_text)
                connection.commit()
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError(f"注册项目失败：{exc}") from exc

    def configured_project_ids(self) -> list[str]:
        try:
            with self._connection() as connection:
                return [row[0] for row in connection.execute("SELECT project_id FROM projects ORDER BY project_id")]
        except sqlite3.Error as exc:
            raise StoreError(f"读取项目列表失败：{exc}") from exc

    def start_refresh(self, project_id: str, scope: str) -> str:
        refresh_id = uuid.uuid4().hex
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO refresh_runs(refresh_id, project_id, scope, status, started_at) "
                    "VALUES (?, ?, ?, 'running', ?)",
                    (refresh_id, project_id, scope, utc_now()),
                )
            return refresh_id
        except sqlite3.Error as exc:
            raise StoreError(f"无法记录刷新开始状态：{exc}") from exc

    def complete_refresh(
        self,
        refresh_id: str,
        project_id: str,
        scope: str,
        entries: Iterable[IndexedEntry],
        *,
        scan_duration_ms: int = 0,
    ) -> dict[str, int]:
        values = list(entries)
        file_count = sum(entry.kind == "file" for entry in values)
        directory_count = sum(entry.kind == "directory" for entry in values)
        indexed_bytes = sum((entry.size or 0) for entry in values if entry.kind == "file")
        scan_duration_ms = max(0, int(scan_duration_ms))
        now = utc_now()
        prefix = f"{scope}/" if scope else ""
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    "SELECT status FROM refresh_runs WHERE refresh_id = ? AND project_id = ?",
                    (refresh_id, project_id),
                ).fetchone()
                if not run or run["status"] != "running":
                    raise StoreError("刷新记录不存在或已结束；清单未更新。")
                if scope:
                    existing = connection.execute(
                        "SELECT path, node_id FROM files WHERE project_id = ? AND (path = ? OR substr(path, 1, ?) = ?)",
                        (project_id, scope, len(prefix), prefix),
                    )
                else:
                    existing = connection.execute("SELECT path, node_id FROM files WHERE project_id = ?", (project_id,))
                seen = {entry.path for entry in values}
                stale = [row for row in existing if row["path"] not in seen]
                for row in stale:
                    if row["node_id"]:
                        connection.execute(
                            "DELETE FROM map_nodes WHERE project_id = ? AND node_id = ? AND type = 'File'",
                            (project_id, row["node_id"]),
                        )
                connection.executemany(
                    "DELETE FROM files WHERE project_id = ? AND path = ?",
                    ((project_id, row["path"]) for row in stale),
                )
                for entry in values:
                    node_id = None
                    if entry.kind == "file":
                        node_id = self._ensure_file_node(connection, project_id, entry.path)
                    else:
                        previous = connection.execute(
                            "SELECT node_id FROM files WHERE project_id = ? AND path = ?",
                            (project_id, entry.path),
                        ).fetchone()
                        if previous and previous["node_id"]:
                            connection.execute(
                                "DELETE FROM map_nodes WHERE project_id = ? AND node_id = ? AND type = 'File'",
                                (project_id, previous["node_id"]),
                            )
                    connection.execute(
                        "INSERT INTO files(project_id, path, type, size, mtime_ns, indexed_at, node_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id, path) DO UPDATE SET "
                        "type = excluded.type, size = excluded.size, mtime_ns = excluded.mtime_ns, "
                        "indexed_at = excluded.indexed_at, node_id = excluded.node_id",
                        (project_id, entry.path, entry.kind, entry.size, entry.mtime_ns, now, node_id),
                    )
                connection.execute(
                    "UPDATE refresh_runs SET status = 'succeeded', finished_at = ?, "
                    "file_count = ?, directory_count = ?, indexed_bytes = ?, scan_duration_ms = ?, "
                    "removed_entry_count = ?, error_code = NULL, error_message = NULL "
                    "WHERE refresh_id = ? AND project_id = ?",
                    (now, file_count, directory_count, indexed_bytes, scan_duration_ms, len(stale), refresh_id, project_id),
                )
                connection.commit()
            return {
                "files": file_count,
                "directories": directory_count,
                "removed": len(stale),
                "indexed_bytes": indexed_bytes,
                "scan_duration_ms": scan_duration_ms,
            }
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError(f"提交清单刷新失败：{exc}") from exc

    def fail_refresh(
        self, refresh_id: str, code: str, message: str, *, scan_duration_ms: int | None = None
    ) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE refresh_runs SET status = 'failed', finished_at = ?, error_code = ?, error_message = ?, "
                    "scan_duration_ms = COALESCE(?, scan_duration_ms) "
                    "WHERE refresh_id = ? AND status = 'running'",
                    (
                        utc_now(),
                        code[:100],
                        message[:MAX_ERROR_MESSAGE_LENGTH],
                        max(0, int(scan_duration_ms)) if scan_duration_ms is not None else None,
                        refresh_id,
                    ),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"无法记录刷新失败：{exc}") from exc

    def project_root(self, project_id: str) -> Path | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT root FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                return Path(row["root"]) if row else None
        except sqlite3.Error as exc:
            raise StoreError(f"读取项目根目录失败：{exc}") from exc

    def refresh_history(
        self, project_id: str, *, limit: int = 20, offset: int = 0
    ) -> dict[str, object]:
        if type(limit) is not int or limit < 1 or limit > MAX_REFRESH_HISTORY_LIMIT:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"limit 必须是 1 到 {MAX_REFRESH_HISTORY_LIMIT} 之间的整数。",
            }
        if type(offset) is not int or offset < 0 or offset > MAX_SQLITE_OFFSET:
            return {"ok": False, "error": "invalid_input", "message": "offset 必须是有效的非负整数。"}
        try:
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if not exists:
                    return {"ok": False, "error": "unknown_project", "message": f"未知 project_id：{project_id}"}
                total = connection.execute(
                    "SELECT COUNT(*) FROM refresh_runs WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT refresh_id, scope, status, started_at, finished_at, file_count, directory_count, "
                    "indexed_bytes, scan_duration_ms, removed_entry_count, error_code, error_message "
                    "FROM refresh_runs WHERE project_id=? "
                    "ORDER BY started_at DESC, refresh_id DESC LIMIT ? OFFSET ?",
                    (project_id, limit, offset),
                ).fetchall()
                runs: list[dict[str, object]] = []
                for row in rows:
                    run = {key: row[key] for key in row.keys()}
                    run["elapsed_ms"] = None
                    if run["finished_at"]:
                        try:
                            started = datetime.fromisoformat(str(run["started_at"]))
                            finished = datetime.fromisoformat(str(run["finished_at"]))
                            run["elapsed_ms"] = max(
                                0, int((finished - started).total_seconds() * 1_000)
                            )
                        except (TypeError, ValueError):
                            run["elapsed_ms"] = None
                    scope = run["scope"]
                    run["scope_truncated"] = isinstance(scope, str) and len(scope) > 512
                    if run["scope_truncated"]:
                        run["scope"] = scope[:511] + "…"
                    message = run["error_message"]
                    run["error_truncated"] = isinstance(message, str) and len(message) > 1_000
                    if run["error_truncated"]:
                        run["error_message"] = message[:999] + "…"
                    runs.append(run)
                result: dict[str, object] = {
                    "ok": True,
                    "project_id": project_id,
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "runs": runs,
                    "next_offset": offset + len(runs) if offset + len(runs) < total else None,
                    "output_limited": False,
                }
                while runs and _json_size(result) > MAX_REFRESH_HISTORY_OUTPUT_BYTES:
                    runs.pop()
                    result["next_offset"] = offset + len(runs) if offset + len(runs) < total else None
                    result["output_limited"] = True
                result["truncated"] = result["next_offset"] is not None
                return result
        except sqlite3.Error as exc:
            raise StoreError(f"读取刷新历史失败：{exc}") from exc

    def _status_one(self, connection: sqlite3.Connection, project_id: str) -> dict[str, object] | None:
        project = connection.execute(
            "SELECT project_id, root, root_key FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not project:
            return None
        counts = connection.execute(
            "SELECT SUM(CASE WHEN type = 'file' THEN 1 ELSE 0 END) AS files, "
            "SUM(CASE WHEN type = 'directory' THEN 1 ELSE 0 END) AS directories "
            "FROM files WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        latest = connection.execute(
            "SELECT refresh_id, scope, status, started_at, finished_at, file_count, directory_count, "
            "indexed_bytes, scan_duration_ms, removed_entry_count, error_code, error_message FROM refresh_runs WHERE project_id = ? "
            "ORDER BY started_at DESC, refresh_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        incomplete = connection.execute(
            "SELECT refresh_id, scope, status, started_at, finished_at, file_count, directory_count, "
            "indexed_bytes, scan_duration_ms, removed_entry_count, error_code, error_message "
            "FROM refresh_runs WHERE project_id = ? AND status = 'running' "
            "ORDER BY started_at DESC, refresh_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        last_success = connection.execute(
            "SELECT refresh_id, scope, started_at, finished_at, file_count, directory_count, "
            "indexed_bytes, scan_duration_ms, removed_entry_count "
            "FROM refresh_runs WHERE project_id = ? AND status = 'succeeded' "
            "ORDER BY finished_at DESC, refresh_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()

        def bounded_text(value: str | None, maximum: int) -> tuple[str | None, bool]:
            if value is None or len(value) <= maximum:
                return value, False
            return value[: maximum - 1] + "…", True

        def refresh_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
            if not row:
                return None
            result = {key: row[key] for key in row.keys()}
            result["scope"], result["scope_truncated"] = bounded_text(result["scope"], 512)
            if "error_message" in result:
                result["error_message"], result["error_truncated"] = bounded_text(
                    result["error_message"], 1_000
                )
            return result

        bounded_root, root_truncated = bounded_text(project["root"], 512)

        return {
            "project_id": project["project_id"],
            "root": bounded_root,
            "root_truncated": root_truncated,
            "root_fingerprint": hashlib.sha256(project["root_key"].encode("utf-8")).hexdigest()[:16],
            "discovered_file_count": counts["files"] or 0,
            "discovered_directory_count": counts["directories"] or 0,
            "latest_refresh": refresh_dict(latest),
            "last_successful_refresh": refresh_dict(last_success),
            "incomplete_refresh": refresh_dict(incomplete),
            "refresh_incomplete": incomplete is not None,
            "incomplete_explanation": (
                "刷新已开始但未记录完成，可能仍在运行，也可能因服务中断未完成；已有文件清单保持上次成功状态。"
                if incomplete is not None else None
            ),
        }

    def status(
        self,
        project_id: str | None = None,
        *,
        project_ids: list[str] | None = None,
    ) -> dict[str, object]:
        try:
            with self._connection() as connection:
                ids = [project_id] if project_id is not None else (
                    project_ids if project_ids is not None else self.configured_project_ids()
                )
                projects = [item for key in ids if (item := self._status_one(connection, key)) is not None]
                if project_id is not None and not projects:
                    return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}"}
                return {"ok": True, "projects": projects}
        except sqlite3.Error as exc:
            raise StoreError(f"读取索引状态失败：{exc}") from exc

    def list_files(
        self,
        project_id: str,
        directory: str = "",
        *,
        include_directories: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> dict[str, object]:
        if type(limit) is not int or limit < 1:
            return {"ok": False, "error": "invalid_input", "message": "limit 必须是正整数。"}
        if type(offset) is not int or offset < 0 or offset > MAX_SQLITE_OFFSET:
            return {"ok": False, "error": "invalid_input", "message": "offset 必须是非负的 SQLite 整数。"}
        if type(include_directories) is not bool:
            return {"ok": False, "error": "invalid_input", "message": "include_directories 必须是布尔值。"}
        effective_limit = min(limit, MAX_LIST_LIMIT)
        prefix = f"{directory}/" if directory else ""
        type_clause = "" if include_directories else " AND type = 'file'"
        scope_clause = "" if not directory else " AND (path = ? OR substr(path, 1, ?) = ?)"
        params: list[object] = [project_id]
        if directory:
            params.extend((directory, len(prefix), prefix))
        try:
            with self._connection() as connection:
                if not connection.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone():
                    return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}"}
                where = f"project_id = ?{scope_clause}{type_clause}"
                total = connection.execute(f"SELECT COUNT(*) FROM files WHERE {where}", params).fetchone()[0]
                rows = connection.execute(
                    "SELECT path, type, size, mtime_ns, indexed_at, node_id FROM files WHERE "
                    f"{where} ORDER BY path COLLATE BINARY LIMIT ? OFFSET ?",
                    [*params, effective_limit, offset],
                ).fetchall()
                entries = [dict(row) for row in rows]
                result: dict[str, object] = {
                    "ok": True,
                    "project_id": project_id,
                    "directory": directory or ".",
                    "include_directories": include_directories,
                    "limit": effective_limit,
                    "offset": offset,
                    "total": total,
                    "entries": entries,
                    "next_offset": None,
                    "output_limited": False,
                    "reason": None,
                }
                byte_limited = False
                while entries and _json_size(result) > MAX_LIST_OUTPUT_BYTES:
                    entries.pop()
                    byte_limited = True
                result["next_offset"] = offset + len(entries) if offset + len(entries) < total else None
                result["output_limited"] = byte_limited
                result["reason"] = "output_budget" if byte_limited else None
                return result
        except sqlite3.Error as exc:
            raise StoreError(f"读取文件清单失败：{exc}") from exc


def sys_platform_is_macos() -> bool:
    import sys

    return sys.platform == "darwin"


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
