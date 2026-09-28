from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from project_preview.filesystem import ProjectFiles
from project_preview.index import ProjectIndex
from project_preview.store import IndexStore


def make_server(
    projects: dict[str, ProjectFiles] | ProjectFiles,
    store: IndexStore | None = None,
    index: ProjectIndex | None = None,
) -> MCPServer:
    if isinstance(projects, ProjectFiles):
        configured_projects = {"default": projects}
    else:
        configured_projects = dict(projects)
    if store is None:
        raise ValueError("make_server 必须接收已经注册项目的 IndexStore；由 CLI 配置服务数据目录。")
    if index is None:
        index = ProjectIndex(configured_projects, store)

    server = MCPServer(
        "project-preview-mcp",
        instructions=(
            "只访问启动参数 --root 或 --project 指定的本地项目。多项目配置时，"
            "browse、preview、search、refresh、refresh_history 和 list_files 必须提供 project_id；"
            "单项目配置可省略。status 可传 project_id 查询一个项目，省略时汇总所有项目。"
            "所有工具路径都相对于所选项目根目录。refresh 写入文件元数据清单，"
            "不会保存源码正文；refresh_history 返回可分页的持久化运行记录和扫描统计。"
            "update_map 只写入 Module/Concept 语义节点和允许类型的关系；"
            "context 返回有完整性标记的局部结构与依据新鲜度；traverse 用于有界多跳路径探索。"
            "resolve_paths 可按项目根目录下的精确路径解析 File 节点 ID。confirmed 写入必须提交 preview 返回的 version_token。"
        ),
    )

    @server.tool()
    def browse(
        directory: str = "", limit: int = 100, offset: int = 0, project_id: str | None = None
    ) -> dict[str, Any]:
        """列出项目内目录的直接子项，返回相对路径、类型和分页信息。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, project = selected
        return _with_project_id(project.browse(directory=directory, limit=limit, offset=offset), selected_id)

    @server.tool()
    def preview(
        path: str, start_line: int = 1, line_count: int = 80, project_id: str | None = None
    ) -> dict[str, Any]:
        """按 1 起始行号预览 UTF-8 文本；返回行号、截断原因和续读信息。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, project = selected
        return _with_project_id(
            index.preview(selected_id, path, start_line=start_line, line_count=line_count), selected_id
        )

    @server.tool()
    def search(
        mode: str,
        query: str,
        directory: str = "",
        limit: int = 20,
        context_lines: int = 0,
        case_sensitive: bool = True,
        offset: int = 0,
        project_id: str | None = None,
        node_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """按路径片段、源码字面量或语义地图名称/别名/摘要搜索。path/source query 最多 512 字符；map 最多 256 字符且默认区分大小写。map 不接受 directory/context_lines，可用 node_types 筛选节点类型。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, project = selected
        if mode == "map":
            if directory or context_lines != 0:
                return {"ok": False, "error": "invalid_input", "message": "map 搜索不接受 directory 或 context_lines。", "project_id": selected_id}
            return index.search_map(
                selected_id, query, limit=limit, offset=offset, case_sensitive=case_sensitive,
                node_types=node_types,
            )
        if node_types is not None:
            return {"ok": False, "error": "invalid_input", "message": "node_types 仅适用于 map 搜索。", "project_id": selected_id}
        if offset != 0:
            return {"ok": False, "error": "invalid_input", "message": "offset 仅适用于 map 搜索。", "project_id": selected_id}
        result = project.search(
            mode=mode,
            query=query,
            directory=directory,
            limit=limit,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )
        return _with_project_id(result, selected_id)

    @server.tool()
    def refresh(directory: str = "", project_id: str | None = None) -> dict[str, Any]:
        """刷新整个项目或指定子目录的文件清单；子目录刷新不会触碰其他目录。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.refresh(selected_id, directory=directory)

    @server.tool()
    def refresh_history(
        limit: int = 20, offset: int = 0, project_id: str | None = None
    ) -> dict[str, Any]:
        """按项目查询可分页的索引构建记录，包括耗时、文件/目录数、元数据字节和失败信息。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.refresh_history(selected_id, limit=limit, offset=offset)

    @server.tool()
    def status(project_id: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """报告文件清单、最近刷新情况、语义关联覆盖和最近 context 检查到的新鲜度观察。"""
        return index.status(project_id, limit=limit, offset=offset)

    @server.tool()
    def list_files(
        directory: str = "",
        limit: int = 100,
        offset: int = 0,
        include_directories: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """查询上次成功刷新的持久化清单，按相对路径稳定排序并分页。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.list_files(
            selected_id,
            directory=directory,
            include_directories=include_directories,
            limit=limit,
            offset=offset,
        )

    @server.tool()
    def update_map(
        upsert_nodes: list[dict[str, Any]] | None = None,
        delete_node_ids: list[str] | None = None,
        upsert_edges: list[dict[str, Any]] | None = None,
        delete_edges: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """原子新增/修改/删除语义节点和关系；dry_run=true 可预检并回滚，不会保存批准票据。confirmed 节点必须附 preview 令牌对应的文件依据。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.update_map(
            selected_id,
            upsert_nodes=upsert_nodes,
            delete_node_ids=delete_node_ids,
            upsert_edges=upsert_edges,
            delete_edges=delete_edges,
            dry_run=dry_run,
        )

    @server.tool()
    def context(
        node_id: str,
        neighbor_limit: int = 10,
        project_id: str | None = None,
        evidence_offset: int = 0,
        evidence_limit: int = 20,
    ) -> dict[str, Any]:
        """按稳定 ID 查询节点、完整性标记、可分页依据、新鲜度与局部结构。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.context(
            selected_id, node_id, neighbor_limit=neighbor_limit,
            evidence_offset=evidence_offset, evidence_limit=evidence_limit,
        )

    @server.tool()
    def traverse(
        start_node_id: str,
        target_node_id: str | None = None,
        relations: list[str] | None = None,
        direction: str = "outgoing",
        max_depth: int = 2,
        node_limit: int = 50,
        edge_limit: int = 100,
        cursor: str | None = None,
        node_types: list[str] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """按关系方向作有界多跳遍历；可限制返回节点类型；默认沿 contains 展开，最大深度 8，每页最多 50 节点/100 条边；用 next_cursor 续读。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.traverse(
            selected_id, start_node_id, target_node_id=target_node_id,
            relations=relations, direction=direction, max_depth=max_depth,
            node_limit=node_limit, edge_limit=edge_limit, cursor=cursor,
            node_types=node_types,
        )

    @server.tool()
    def resolve_paths(paths: list[str], project_id: str | None = None) -> dict[str, Any]:
        """在指定项目内解析精确相对路径并返回已索引文件节点 ID。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.resolve_paths(selected_id, paths)

    return server


def _select_project(
    index: ProjectIndex, project_id: str | None
) -> tuple[str, ProjectFiles] | dict[str, Any]:
    if project_id is None:
        project_ids = index.project_ids()
        if len(project_ids) == 1:
            project_id = project_ids[0]
        else:
            return {
                "ok": False,
                "error": "project_id_required",
                "message": "配置了多个项目，请提供 project_id。",
                "available_project_ids": project_ids,
            }
    project = index.projects.get(project_id)
    if project is None:
        return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}", "project_id": project_id}
    return project_id, project


def _with_project_id(result: dict[str, Any], project_id: str) -> dict[str, Any]:
    return {**result, "project_id": project_id}
