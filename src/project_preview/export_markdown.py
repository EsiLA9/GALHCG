from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from project_preview.filesystem import (
    MAX_PREVIEW_LINES,
    ProjectFiles,
    ProjectSetupError,
    ToolFailure,
)


_LANGUAGES = {
    ".bash": "bash",
    ".bat": "bat",
    ".c": "c",
    ".cc": "cpp",
    ".cfg": "ini",
    ".cmd": "bat",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".csv": "csv",
    ".go": "go",
    ".html": "html",
    ".ini": "ini",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".md": "markdown",
    ".py": "python",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def _read_all_preview_lines(project: ProjectFiles, path: str) -> tuple[str | None, str | None]:
    """Read a supported text file through the same safe, bounded preview path as MCP."""
    collected: list[str] = []
    start_line = 1
    while True:
        result = project.preview(path=path, start_line=start_line, line_count=MAX_PREVIEW_LINES)
        if not result.get("ok"):
            return None, str(result.get("message") or result.get("error") or "读取失败")

        collected.extend(line["content"] for line in result.get("lines", []))
        reason = result.get("reason")
        if not result.get("truncated") and reason in {None, "file_end"}:
            return "\n".join(collected), None

        next_line = result.get("next_start_line")
        if reason not in {"line_count", "output_budget"} or type(next_line) is not int or next_line <= start_line:
            detail = result.get("continuation") or f"文件内容无法完整读取（{reason or '未知原因'}）"
            return None, str(detail)
        start_line = next_line


def _render_file(path: str, content: str) -> str:
    fence_length = max(3, max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=2) + 1)
    fence = "`" * fence_length
    language = _LANGUAGES.get(Path(path).suffix.lower(), "")
    heading = html.escape(path, quote=False)
    body = content
    if body and not body.endswith("\n"):
        body += "\n"
    return f"## {heading}\n\n{fence}{language}\n{body}{fence}\n\n"


def export_project(root: Path, output: Path | None = None) -> dict[str, object]:
    """Export all supported, non-ignored project text files into one Markdown document."""
    if not root.is_absolute():
        raise ValueError("--root 必须是绝对路径。")
    project = ProjectFiles(root)
    output_path = output.expanduser().resolve(strict=False) if output else (
        project.root.parent / f"{project.root.name or 'project'}-contents.md"
    )
    if output_path.suffix.lower() != ".md":
        raise ValueError("--output 必须以 .md 结尾。")
    try:
        output_path.relative_to(project.root)
    except ValueError:
        pass
    else:
        raise ValueError("导出文件必须放在项目根目录之外，避免被再次纳入项目内容。")

    _scope, entries = project.scan_manifest()
    files = [entry for entry in entries if entry.kind == "file"]
    exported: list[tuple[str, str, int | None]] = []
    skipped: list[tuple[str, str]] = []
    for entry in files:
        content, error = _read_all_preview_lines(project, entry.path)
        if error is not None or content is None:
            skipped.append((entry.path, error or "无法读取文件"))
            continue
        exported.append((entry.path, content, entry.size))

    lines = [
        f"# {html.escape(project.root.name or 'Project')} 项目内容导出",
        "",
        f"- 导出时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 纳入文件：{len(exported)} / {len(files)}",
        "- 读取规则：遵循项目 `.gitignore` 和默认排除规则；不跟随链接；内容以 UTF-8 文本读取。",
        "",
        "## 文件目录",
        "",
    ]
    lines.extend(f"- `{html.escape(path, quote=False)}`" for path, _content, _size in exported)
    if skipped:
        lines.extend(["", "## 未能导出的文件", ""])
        lines.extend(f"- `{html.escape(path, quote=False)}`：{html.escape(reason, quote=False)}" for path, reason in skipped)
    lines.extend(["", "## 文件内容", ""])
    document = "\n".join(lines)
    if not document.endswith("\n"):
        document += "\n"
    document += "".join(_render_file(path, content) for path, content, _size in exported)

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

    return {
        "output": output_path,
        "file_count": len(files),
        "exported_count": len(exported),
        "skipped": skipped,
        "bytes_written": output_path.stat().st_size,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-preview-export-md",
        description="将项目内可读取的文本文件汇总导出到一个 Markdown 文件（独立于 MCP 服务）。",
    )
    parser.add_argument("--root", required=True, help="项目根目录的绝对路径。")
    parser.add_argument("--output", help="导出 Markdown 的路径；默认写到项目根目录旁。")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    output = Path(args.output).expanduser() if args.output else None
    try:
        result = export_project(root, output)
    except (ProjectSetupError, ToolFailure, OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"已导出 {result['exported_count']}/{result['file_count']} 个文件：{result['output']}")
    print(f"Markdown 大小：{result['bytes_written']} 字节")
    for path, reason in result["skipped"]:
        print(f"未导出 {path}：{reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
