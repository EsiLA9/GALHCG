from __future__ import annotations

import os
from pathlib import Path
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
            "面向本机 Coding Agent 与本地语义审阅界面使用。访问范围由启动参数 --root 或 --project 注册的项目根目录决定；"
            "project_id 用于选择项目及其数据库命名空间，不是用户角色或登录权限。多项目配置时，"
            "browse、preview、search、refresh、refresh_history 和 list_files 必须提供 project_id；"
            "单项目配置可省略。status 可传 project_id 查询一个项目，省略时汇总所有项目。"
            "所有工具路径都相对于所选项目根目录。refresh 写入文件元数据清单，"
            "不会保存源码正文；refresh_history 返回可分页的持久化运行记录和扫描统计。"
            "update_map 只写入 Module/Concept 语义节点和允许类型的关系；"
            "context 返回有完整性标记的局部结构与依据新鲜度；traverse 用于有界多跳路径探索。"
            "review_changes 只读检查有直接语义引用的文件版本，返回变化状态和直接 Evidence/maps_to 邻接，不扩展 Concept 多跳；"
            "verify_freshness 是单独的显式操作，会将一个节点或关系的最新核验观察写入派生状态。"
            "resolve_paths 可按项目根目录下的精确路径解析 File 节点 ID。confirmed 写入必须提交 preview 返回的 version_token。"
            "resolve_project 可根据调用方显式提供的绝对工作目录匹配已注册项目根；它不会读取客户端当前目录，也不会改变后续工具的项目选择，调用方仍应传返回的 project_id。"
            "MCP 与本地审阅界面省略 --data-dir 时使用相同的当前用户默认目录；如使用自定义目录，两边传入同一个 --data-dir 即可，无需另设环境变量。"
        ),
    )

    @server.tool()
    def resolve_project(workspace_path: str) -> dict[str, Any]:
        """按调用方显式传入的绝对工作目录匹配已配置项目；不读取 MCP 客户端 cwd、不注册项目，也不改变后续调用的 project_id。匹配后请在后续工具中显式传回结果的 project_id。"""
        return _resolve_project_by_workspace_path(index, workspace_path)

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
        """搜索 path、source 或 map。path 按路径片段查找，source 搜源码字面量；map 查节点名称、别名和摘要，未筛选时也可能返回 File。只看概念时传 node_types=["Concept"]；map 不接受 directory/context_lines。path/source 查询最多 512 字符，map 最多 256 字符；搜索可能受预算截断，检查完整性字段并按提示缩小范围。"""
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
    def review_changes(
        directory: str = "",
        path: str = "",
        limit: int = 20,
        offset: int = 0,
        owner_offset: int = 0,
        owner_limit: int = 5,
        include_unchanged: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """只读复核项目中有直接 Evidence 或 maps_to 关联的文件。version_token 用于确认内容变化，mtime 仅作元数据线索。每页最多检查 20 个文件、每文件展示 5 个直接 owner；用 next_offset 和 owner_next_offset 续读。path 与 directory 不能同时指定。查询不会刷新文件清单、写入 freshness 或修改语义图。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.review_changes(
            selected_id,
            directory=directory,
            path=path,
            limit=limit,
            offset=offset,
            owner_offset=owner_offset,
            owner_limit=owner_limit,
            include_unchanged=include_unchanged,
        )

    @server.tool()
    def verify_freshness(owner_type: str, owner_id: str, project_id: str | None = None) -> dict[str, Any]:
        """显式重查一个语义节点或关系的 Evidence，并写入其最新 freshness 观察；不会改动节点、关系或 Evidence。"""
        selected = _select_project(index, project_id)
        if isinstance(selected, dict):
            return selected
        selected_id, _project = selected
        return index.verify_freshness(selected_id, owner_type, owner_id)

    @server.tool()
    def list_files(
        directory: str = "",
        limit: int = 100,
        offset: int = 0,
        include_directories: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """查询上次成功刷新的持久化清单，按相对路径稳定排序并分页。输出预算可能使实际条数少于 limit；始终按响应的 next_offset 续读，直到 next_offset 为 null，不要按请求 limit 自行递增。"""
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
        """原子新增/修改/删除语义节点和关系。节点字段：id/type/name/summary/aliases/state/evidence；type 仅 Module 或 Concept，state 仅 tentative 或 confirmed（字段名是 state，不是 status）。边字段：source_id/relation/target_id/evidence；relation 为 contains/maps_to/depends_on/related_to，maps_to 可带 roles（implementation/documentation/test/unspecified）。依据项严格为 {path, version_token}，令牌来自 preview。confirmed 节点须带依据。每次 update_map 合计最多校验 32 条节点与边依据，同一路径由不同 owner 引用仍分别计数；超限时拆分批次。先用 dry_run=true 预检，再提交正式批次。"""
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


def _resolve_project_by_workspace_path(
    index: ProjectIndex, workspace_path: str
) -> dict[str, Any]:
    if not isinstance(workspace_path, str) or not workspace_path.strip() or "\x00" in workspace_path:
        return {
            "ok": False,
            "error": "invalid_workspace_path",
            "message": "workspace_path 必须是非空的绝对目录路径。",
        }
    try:
        supplied_path = Path(workspace_path).expanduser()
    except (OSError, RuntimeError, ValueError):
        return {
            "ok": False,
            "error": "invalid_workspace_path",
            "message": "workspace_path 无法解析为本机目录路径。",
        }
    if not supplied_path.is_absolute():
        return {
            "ok": False,
            "error": "workspace_path_not_absolute",
            "message": "workspace_path 必须是绝对路径；请传入 Agent 的项目工作目录。",
        }
    try:
        resolved_path = supplied_path.resolve(strict=True)
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "workspace_path_not_found",
            "message": "workspace_path 不存在，请检查 Agent 当前工作目录。",
        }
    except (OSError, RuntimeError, ValueError):
        return {
            "ok": False,
            "error": "workspace_path_unavailable",
            "message": "workspace_path 无法解析；请检查路径或符号链接。",
        }
    if not resolved_path.is_dir():
        return {
            "ok": False,
            "error": "workspace_path_not_directory",
            "message": "workspace_path 必须指向目录。",
        }

    matches: list[tuple[int, str, str]] = []
    for project_id, project in index.projects.items():
        root_path = project.root
        try:
            common_path = os.path.commonpath((str(resolved_path), str(root_path)))
        except ValueError:
            # Windows paths on different drives do not share a common path.
            continue
        if os.path.normcase(os.path.normpath(common_path)) != os.path.normcase(os.path.normpath(str(root_path))):
            continue
        matches.append((len(root_path.parts), project_id, str(root_path)))

    if not matches:
        return {
            "ok": False,
            "error": "workspace_not_configured",
            "message": "Agent 工作目录不在任何已配置项目根目录内；请将其映射到服务已登记的项目。",
            "workspace_path": str(resolved_path),
            "available_project_ids": index.project_ids(),
        }

    deepest_root = max(depth for depth, _project_id, _root in matches)
    best_matches = [match for match in matches if match[0] == deepest_root]
    if len(best_matches) != 1:
        return {
            "ok": False,
            "error": "ambiguous_workspace_path",
            "message": "此工作目录匹配多个同等深度的已配置项目；请显式选择 project_id。",
            "workspace_path": str(resolved_path),
            "candidates": [
                {"project_id": project_id, "root_path": root_path}
                for _depth, project_id, root_path in best_matches
            ],
        }

    _depth, project_id, root_path = best_matches[0]
    return {
        "ok": True,
        "project_id": project_id,
        "root_path": root_path,
        "workspace_path": str(resolved_path),
        "match_type": "project_root" if os.path.normcase(root_path) == os.path.normcase(str(resolved_path)) else "subdirectory",
        "selection_is_persistent": False,
    }


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
