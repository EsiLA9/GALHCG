from __future__ import annotations

import argparse
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_preview.store import MAX_REFRESH_HISTORY_LIMIT, IndexStore, validate_project_id


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _load_all_runs(store: IndexStore, project_id: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.refresh_history(
            project_id, limit=MAX_REFRESH_HISTORY_LIMIT, offset=offset
        )
        if not page.get("ok"):
            raise ValueError(str(page.get("message") or "无法读取刷新历史。"))
        page_runs = page.get("runs", [])
        runs.extend(page_runs)
        next_offset = page.get("next_offset")
        if next_offset is None:
            return runs
        if type(next_offset) is not int or next_offset <= offset:
            raise ValueError("刷新历史分页游标无效。")
        offset = next_offset


def render_history_markdown(project_id: str, runs: list[dict[str, Any]]) -> str:
    counts = Counter(str(run.get("status")) for run in runs)
    completed_elapsed = [
        run["elapsed_ms"]
        for run in runs
        if run.get("status") == "succeeded" and type(run.get("elapsed_ms")) is int
    ]
    completed_scan = [
        run["scan_duration_ms"]
        for run in runs
        if run.get("status") == "succeeded" and type(run.get("scan_duration_ms")) is int
    ]
    total_indexed_bytes = sum(
        run.get("indexed_bytes") or 0 for run in runs if run.get("status") == "succeeded"
    )
    lines = [
        f"# {project_id} 索引构建记录",
        "",
        f"- 导出时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 构建次数：{len(runs)}（成功 {counts['succeeded']}、失败 {counts['failed']}、未完成 {counts['running']}）",
        f"- 成功扫描累计文件元数据字节：{total_indexed_bytes}",
        (
            f"- 成功构建总耗时（ms，开始/结束时间差）：最短 {min(completed_elapsed)}、"
            f"平均 {sum(completed_elapsed) // len(completed_elapsed)}、最长 {max(completed_elapsed)}"
            if completed_elapsed else "- 成功构建总耗时：暂无成功记录"
        ),
        (
            f"- 清单扫描耗时（ms）：最短 {min(completed_scan)}、"
            f"平均 {sum(completed_scan) // len(completed_scan)}、最长 {max(completed_scan)}"
            if completed_scan else "- 清单扫描耗时：暂无成功记录"
        ),
        "- 这里只记录扫描统计与错误摘要，不保存源码正文；统计字节量来自文件大小元数据，不代表读取的源码字节量。",
        "",
        "| 时间（UTC） | 状态 | 范围 | 文件数 | 目录数 | 元数据字节 | 总耗时（ms） | 扫描耗时（ms） | 移除项 | 错误 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for run in runs:
        error = ""
        if run.get("error_code"):
            error = str(run["error_code"])
            if run.get("error_message"):
                error += ": " + str(run["error_message"])
        lines.append(
            "| " + " | ".join(
                (
                    _cell(run.get("finished_at") or run.get("started_at")),
                    _cell(run.get("status")),
                    f"`{_cell(run.get('scope') or '.')}`",
                    _cell(run.get("file_count")),
                    _cell(run.get("directory_count")),
                    _cell(run.get("indexed_bytes")),
                    _cell(run.get("elapsed_ms")),
                    _cell(run.get("scan_duration_ms")),
                    _cell(run.get("removed_entry_count")),
                    _cell(error),
                )
            ) + " |"
        )
        lines.append(f"<!-- refresh_id: {_cell(run.get('refresh_id'))} -->")
    lines.extend([
        "",
        "## 指标说明",
        "",
        "- `indexed_bytes` 是本次成功扫描发现的普通文件大小总和；局部刷新只统计该刷新范围。",
        "- `elapsed_ms` 根据持久化的开始/结束时间计算，反映构建的近似总耗时。",
        "- `scan_duration_ms` 用单调时钟测量清单扫描耗时，不包含数据库事务提交时间。",
        "- `removed_entry_count` 是本次刷新从持久化清单移除的文件或目录项数量。",
        "- 报告保存于保留目录 `.project-preview-mcp/`；服务默认不会把该目录再次纳入项目索引。",
        "",
    ])
    return "\n".join(lines)


def export_history(
    project_id: str, store: IndexStore, output: Path | None = None
) -> dict[str, Any]:
    validate_project_id(project_id)
    root = store.project_root(project_id)
    if root is None:
        raise ValueError(f"索引数据库中没有 project_id：{project_id}")
    root = root.expanduser().resolve(strict=False)
    if output is None:
        output_path = root / ".project-preview-mcp" / "refresh-history.md"
    else:
        output_path = output.expanduser()
        if not output_path.is_absolute():
            output_path = root / output_path
        output_path = output_path.resolve(strict=False)
    if output_path.suffix.lower() != ".md":
        raise ValueError("--output 必须以 .md 结尾。")
    try:
        output_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("历史报告必须保存在所选项目目录内。") from exc

    runs = _load_all_runs(store, project_id)
    document = render_history_markdown(project_id, runs)
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
            stream.write(document)
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {"output": output_path, "run_count": len(runs), "bytes_written": output_path.stat().st_size}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-preview-export-history-md",
        description="读取持久化刷新记录并在目标项目内生成 Markdown 分析报告；不启动 MCP。",
    )
    parser.add_argument("--project-id", default="default", help="SQLite 中注册的项目 ID；默认 default。")
    parser.add_argument("--data-dir", help="服务 SQLite 数据目录；默认使用当前用户数据目录。")
    parser.add_argument("--output", help="项目内 Markdown 输出路径；默认 .project-preview-mcp/refresh-history.md。")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else IndexStore.default_data_dir()
    output = Path(args.output).expanduser() if args.output else None
    try:
        store = IndexStore(data_dir)
        result = export_history(args.project_id, store, output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"已导出 {result['run_count']} 条索引构建记录：{result['output']}（{result['bytes_written']} 字节）")


if __name__ == "__main__":
    main()
