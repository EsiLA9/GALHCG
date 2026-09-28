from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from project_preview.filesystem import ProjectFiles, ProjectSetupError
from project_preview.index import ProjectIndex
from project_preview.server import make_server
from project_preview.store import IndexStore, StoreError, validate_project_id


def _parser(
    *, prog: str = "project-preview-mcp", description: str = "为 Coding Agent 提供本地项目浏览、搜索、预览及持久化文件清单。"
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--root",
        help="单项目兼容模式；要访问的项目根目录，必须是绝对路径，项目 ID 固定为 default。",
    )
    selection.add_argument(
        "--project",
        action="append",
        metavar="ID=PATH",
        help="注册一个项目；可重复指定，ID 稳定且每个 ID 永久绑定一个工作目录。",
    )
    parser.add_argument(
        "--data-dir",
        help="服务独立数据目录（绝对路径）；默认使用当前用户的数据目录。",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="应用于已配置项目的额外项目相对 Git 忽略式排除规则；可重复指定。",
    )
    return parser


def _project_specs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[tuple[str, Path]]:
    if args.root is not None:
        return [("default", Path(args.root))]
    specs: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for value in args.project or []:
        if "=" not in value:
            parser.error(f"--project 格式必须是 ID=PATH：{value}")
        project_id, root_text = value.split("=", 1)
        try:
            validate_project_id(project_id)
        except StoreError as exc:
            parser.error(str(exc))
        if project_id in seen:
            parser.error(f"重复的 project_id：{project_id}")
        seen.add(project_id)
        specs.append((project_id, Path(root_text)))
    if not specs:
        parser.error("至少指定一个 --project ID=PATH。")
    return specs


def create_runtime(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[
    dict[str, ProjectFiles], IndexStore, ProjectIndex
]:
    try:
        specs = _project_specs(args, parser)
        projects: dict[str, ProjectFiles] = {}
        for project_id, root in specs:
            projects[project_id] = ProjectFiles(root, extra_excludes=args.exclude)

        data_dir = Path(args.data_dir) if args.data_dir else IndexStore.default_data_dir()
        if not data_dir.is_absolute():
            raise StoreError("--data-dir 必须是绝对路径。")
        normalized_data_dir = data_dir.resolve(strict=False)
        for project_id, project in projects.items():
            try:
                normalized_data_dir.relative_to(project.root)
            except ValueError:
                continue
            raise StoreError(
                f"服务数据目录位于项目 '{project_id}' 根目录内；请将 --data-dir 放到所有项目之外，"
                "避免把索引写入或扫描进源码项目。"
            )

        store = IndexStore(normalized_data_dir)
        for project_id, project in projects.items():
            store.register_project(project_id, project.root)
        return projects, store, ProjectIndex(projects, store)
    except (ProjectSetupError, StoreError, OSError, ValueError) as exc:
        parser.error(str(exc))
        raise AssertionError("ArgumentParser.error should exit")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = _parser()
    args = parser.parse_args()
    projects, store, index = create_runtime(args, parser)
    server = make_server(projects, store, index)
    server.run(transport="stdio")
