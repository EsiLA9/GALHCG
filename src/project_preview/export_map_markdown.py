from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_preview.store import IndexStore, validate_project_id


_TYPE_ORDER = {"Project": 0, "Module": 1, "Concept": 2, "File": 3}
_RELATION_LABELS = {
    "contains": "包含",
    "depends_on": "依赖",
    "maps_to": "映射到",
    "related_to": "相关（无向）",
}


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _evidence_rows(connection: sqlite3.Connection, table: str, project_id: str) -> list[dict[str, Any]]:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    owner = "node_id" if table == "node_evidence" else "edge_id"
    if "file_path" in columns:
        query = (
            f"SELECT {owner} AS owner_id, file_path AS path, created_at "
            f"FROM {table} WHERE project_id=? ORDER BY {owner}, file_path"
        )
    elif "file_node_id" in columns:
        query = (
            f"SELECT e.{owner} AS owner_id, f.path AS path, e.created_at "
            f"FROM {table} AS e JOIN map_nodes AS f "
            "ON f.project_id=e.project_id AND f.node_id=e.file_node_id AND f.type='File' "
            f"WHERE e.project_id=? ORDER BY e.{owner}, f.path"
        )
    else:
        raise ValueError(f"无法识别 SQLite 表 {table} 的证据结构。")
    return [dict(row) for row in connection.execute(query, (project_id,))]


def read_graph_snapshot(database: Path, project_id: str) -> dict[str, Any]:
    if not database.is_file():
        raise FileNotFoundError(f"找不到项目图数据库：{database}")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        project = connection.execute(
            "SELECT project_id, root, created_at FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise ValueError(f"图数据库中没有 project_id：{project_id}")

        node_rows = connection.execute(
            "SELECT node_id, type, path, name, summary, aliases_json, state, managed, created_at, updated_at "
            "FROM map_nodes WHERE project_id=? "
            "ORDER BY CASE type WHEN 'Project' THEN 0 WHEN 'Module' THEN 1 WHEN 'Concept' THEN 2 ELSE 3 END, "
            "name COLLATE NOCASE, node_id",
            (project_id,),
        ).fetchall()
        nodes = [dict(row) for row in node_rows]
        node_evidence = _evidence_rows(connection, "node_evidence", project_id)
        evidence_by_node: dict[str, list[str]] = {}
        for item in node_evidence:
            evidence_by_node.setdefault(item["owner_id"], []).append(item["path"])
        for node in nodes:
            try:
                node["aliases"] = json.loads(node.pop("aliases_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                node["aliases"] = []
            node["evidence"] = evidence_by_node.get(node["node_id"], [])

        edge_columns = {row["name"] for row in connection.execute("PRAGMA table_info(map_edges)")}
        roles_column = "e.roles_json" if "roles_json" in edge_columns else "'[]' AS roles_json"
        edge_rows = connection.execute(
            "SELECT e.edge_id, e.source_id, e.relation, e.target_id, e.managed, e.created_at, e.updated_at, "
            f"{roles_column}, "
            "s.name AS source_name, s.type AS source_type, t.name AS target_name, t.type AS target_type "
            "FROM map_edges AS e "
            "LEFT JOIN map_nodes AS s ON s.project_id=e.project_id AND s.node_id=e.source_id "
            "LEFT JOIN map_nodes AS t ON t.project_id=e.project_id AND t.node_id=e.target_id "
            "WHERE e.project_id=? "
            "ORDER BY e.relation, e.source_id, e.target_id, e.edge_id",
            (project_id,),
        ).fetchall()
        edges = [dict(row) for row in edge_rows]
        for edge in edges:
            try:
                edge["roles"] = json.loads(edge.pop("roles_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                edge["roles"] = []
            if edge["relation"] == "maps_to" and not edge["roles"]:
                edge["roles"] = ["unspecified"]
        edge_evidence = _evidence_rows(connection, "edge_evidence", project_id)
        evidence_by_edge: dict[str, list[str]] = {}
        for item in edge_evidence:
            evidence_by_edge.setdefault(item["owner_id"], []).append(item["path"])
        for edge in edges:
            edge["evidence"] = evidence_by_edge.get(edge["edge_id"], [])

        file_columns = {row["name"] for row in connection.execute("PRAGMA table_info(files)")}
        node_id_column = "node_id" if "node_id" in file_columns else "NULL AS node_id"
        file_rows = connection.execute(
            f"SELECT path, type, size, mtime_ns, indexed_at, {node_id_column} "
            "FROM files WHERE project_id=? ORDER BY path",
            (project_id,),
        ).fetchall()
        files = [dict(row) for row in file_rows]

        latest_refresh = connection.execute(
            "SELECT refresh_id, scope, status, started_at, finished_at, file_count, directory_count, error_code "
            "FROM refresh_runs WHERE project_id=? ORDER BY started_at DESC, refresh_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        schema_row = connection.execute(
            "SELECT schema_version FROM schema_meta WHERE singleton=1"
        ).fetchone()
        return {
            "project": dict(project),
            "nodes": nodes,
            "edges": edges,
            "files": files,
            "latest_refresh": dict(latest_refresh) if latest_refresh else None,
            "schema_version": int(schema_row[0]) if schema_row else None,
            "node_evidence_count": len(node_evidence),
            "edge_evidence_count": len(edge_evidence),
        }
    except sqlite3.Error as exc:
        raise ValueError(f"读取 SQLite 图数据库失败：{exc}") from exc
    finally:
        connection.close()


def _mermaid(snapshot: dict[str, Any]) -> str:
    nodes = snapshot["nodes"]
    edges = snapshot["edges"]
    aliases = {node["node_id"]: f"n{index:03d}" for index, node in enumerate(nodes, start=1)}
    lines = ["```mermaid", "flowchart LR"]
    for node in nodes:
        label = html.escape(f"{node['name']} ({node['type']})", quote=True)
        if node["type"] in {"Module", "Concept"} and node["summary"]:
            label += "<br/>" + html.escape(node["summary"][:90], quote=True).replace("\n", "<br/>")
        lines.append(f'  {aliases[node["node_id"]]}["{label}"]')
    for edge in edges:
        source = aliases.get(edge["source_id"])
        target = aliases.get(edge["target_id"])
        if source is None or target is None:
            continue
        label = _RELATION_LABELS.get(edge["relation"], edge["relation"])
        connector = "---" if edge["relation"] == "related_to" else "-->"
        lines.append(f"  {source} {connector}|{label}| {target}")
    lines.append("```")
    return "\n".join(lines)


def render_graph_markdown(snapshot: dict[str, Any]) -> str:
    project_id = snapshot["project"]["project_id"]
    nodes = snapshot["nodes"]
    edges = snapshot["edges"]
    files = snapshot["files"]
    nodes_by_id = {node["node_id"]: node for node in nodes}
    node_counts = Counter(node["type"] for node in nodes)
    edge_counts = Counter(edge["relation"] for edge in edges)
    semantic_nodes = [node for node in nodes if node["type"] in {"Module", "Concept"}]
    summary_chars = sum(len(node["summary"] or "") for node in semantic_nodes)
    file_count = sum(item["type"] == "file" for item in files)
    directory_count = sum(item["type"] == "directory" for item in files)
    project_node = next((node for node in nodes if node["type"] == "Project"), None)
    project_name = project_node["name"] if project_node else project_id

    parts = [
        f"# {project_name} — 图存储导出",
        "",
        f"- Project ID：`{_cell(project_id)}`",
        f"- 导出时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 图节点：{len(nodes)}（" + "、".join(f"{kind} {count}" for kind, count in sorted(node_counts.items(), key=lambda item: _TYPE_ORDER.get(item[0], 99))) + ")",
        f"- 图关系：{len(edges)}（" + "、".join(f"{_RELATION_LABELS.get(kind, kind)} {count}" for kind, count in sorted(edge_counts.items())) + ")",
        f"- 文件清单：{file_count} 个文件、{directory_count} 个目录",
        f"- Module/Concept 摘要：{len(semantic_nodes)} 条，共 {summary_chars} 个字符",
        "- 此导出只读 SQLite；包含图节点、短摘要、文件清单、关系和依据路径，不包含源码正文或不透明版本令牌。",
        "",
        "## 语义摘要层（图中的压缩结构）",
        "",
        "| 类型 | 名称 | 摘要长度 | 摘要 | 别名 | 依据文件 |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for node in semantic_nodes:
        parts.append(
            "| " + " | ".join(
                (
                    _cell(node["type"]),
                    _cell(node["name"]),
                    str(len(node["summary"] or "")),
                    _cell(node["summary"]),
                    _cell(", ".join(node["aliases"])),
                    _cell(", ".join(node["evidence"])),
                )
            ) + " |"
        )
    parts.extend([
        "",
        "摘要长度是语义图中 `summary` 字段的字符数；它显示已存储的概括层，不代表自动计算的源码压缩率。",
        "",
        "## Mermaid 关系图",
        "",
        _mermaid(snapshot),
        "",
        "## 全部节点",
        "",
        "| 类型 | 节点 ID | 名称 | 状态 | 路径 | 摘要 | 别名 | 依据文件 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for node in nodes:
        parts.append(
            "| " + " | ".join(
                (
                    _cell(node["type"]),
                    f"`{_cell(node['node_id'])}`",
                    _cell(node["name"]),
                    _cell(node["state"] or ""),
                    _cell(node["path"] or ""),
                    _cell(node["summary"]),
                    _cell(", ".join(node["aliases"])),
                    _cell(", ".join(node["evidence"])),
                )
            ) + " |"
        )
    parts.extend([
        "",
        "## 全部关系",
        "",
        "| 来源 | 关系 | 目标 | 文件角色 | 管理方式 | 依据文件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for edge in edges:
        source_name = nodes_by_id.get(edge["source_id"], {}).get("name", edge["source_id"])
        target_name = nodes_by_id.get(edge["target_id"], {}).get("name", edge["target_id"])
        managed = "服务维护" if edge["managed"] else "语义图"
        relation = _RELATION_LABELS.get(edge["relation"], edge["relation"])
        parts.append(
            "| " + " | ".join(
                (
                    f"{_cell(source_name)} (`{_cell(edge['source_id'])}`)",
                    f"{relation} (`{_cell(edge['relation'])}`)",
                    f"{_cell(target_name)} (`{_cell(edge['target_id'])}`)",
                    _cell(", ".join(edge.get("roles", []))),
                    managed,
                    _cell(", ".join(edge["evidence"])),
                )
            ) + " |"
        )
    parts.extend([
        "",
        "## 文件元数据清单",
        "",
        "| 类型 | 路径 | 大小（字节） | 修改时间（ns） | 已索引时间 | File 节点 ID |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ])
    for item in files:
        parts.append(
            "| " + " | ".join(
                (
                    _cell(item["type"]),
                    f"`{_cell(item['path'])}`",
                    _cell(item["size"]),
                    _cell(item["mtime_ns"]),
                    _cell(item["indexed_at"]),
                    f"`{_cell(item['node_id'])}`" if item["node_id"] else "",
                )
            ) + " |"
        )
    refresh = snapshot["latest_refresh"]
    parts.extend(["", "## 最近一次刷新", ""])
    if refresh:
        parts.extend([
            f"- 状态：`{_cell(refresh['status'])}`",
            f"- 范围：`{_cell(refresh['scope'] or '.')}`",
            f"- 文件/目录：{refresh['file_count']} / {refresh['directory_count']}",
            f"- 完成时间：{_cell(refresh['finished_at'] or refresh['started_at'])}",
        ])
    else:
        parts.append("尚无刷新记录。")
    parts.extend([
        "",
        "## 图数据说明",
        "",
        f"- SQLite schema 版本：{snapshot['schema_version']}",
        f"- 节点依据：{snapshot['node_evidence_count']} 条；关系依据：{snapshot['edge_evidence_count']} 条。",
        "- 依据表保存相对路径与不透明版本令牌；本文只显示相对路径，令牌和版本密钥不会导出。",
        "- `contains` 表示结构归属；`depends_on` 表示模块依赖；`maps_to` 将概念映射到文件；`related_to` 是无向关系。",
        "",
    ])
    return "\n".join(parts)


def export_graph(project_id: str, data_dir: Path, output: Path | None = None) -> dict[str, Any]:
    validate_project_id(project_id)
    if not data_dir.is_absolute():
        raise ValueError("--data-dir 必须是绝对路径。")
    database = data_dir.expanduser().resolve(strict=False) / "project-preview.sqlite3"
    snapshot = read_graph_snapshot(database, project_id)
    project_root = Path(snapshot["project"]["root"]).expanduser().resolve(strict=False)
    output_path = output.expanduser().resolve(strict=False) if output else (
        project_root.parent / f"{project_root.name or project_id}-graph.md"
    )
    if output_path.suffix.lower() != ".md":
        raise ValueError("--output 必须以 .md 结尾。")
    try:
        output_path.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ValueError("图导出文件必须放在项目根目录之外。")

    markdown = render_graph_markdown(snapshot)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(markdown)
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return {
        "output": output_path,
        "node_count": len(snapshot["nodes"]),
        "edge_count": len(snapshot["edges"]),
        "semantic_node_count": sum(node["type"] in {"Module", "Concept"} for node in snapshot["nodes"]),
        "bytes_written": output_path.stat().st_size,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-preview-export-map-md",
        description="只读 SQLite 图存储并导出节点、语义摘要、关系和文件清单到 Markdown；不启动 MCP。",
    )
    parser.add_argument("--project-id", default="default", help="SQLite 中注册的项目 ID；默认 default。")
    parser.add_argument("--data-dir", help="服务 SQLite 数据目录；默认使用当前用户数据目录。")
    parser.add_argument("--output", help="Markdown 输出路径；默认写到该项目根目录旁。")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else IndexStore.default_data_dir()
    output = Path(args.output).expanduser() if args.output else None
    try:
        result = export_graph(args.project_id, data_dir, output)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(
        f"已导出项目 {args.project_id} 的图：{result['node_count']} 个节点、"
        f"{result['edge_count']} 条关系、{result['semantic_node_count']} 个语义摘要；"
        f"输出 {result['bytes_written']} 字节：{result['output']}"
    )


if __name__ == "__main__":
    main()
