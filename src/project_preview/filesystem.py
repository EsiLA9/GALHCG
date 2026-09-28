from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pathspec import GitIgnoreSpec

from project_preview.store import IndexedEntry
from project_preview.versions import (
    SnapshotResult,
    VersionResult,
    capture_file_signature,
    hash_file_version,
    read_versioned_snapshot,
)


DEFAULT_EXCLUDES = (
    "**/.git/",
    "**/.hg/",
    "**/.svn/",
    "**/.project-preview-mcp/",
    "**/node_modules/",
    "**/.venv/",
    "**/venv/",
    "**/__pycache__/",
    "**/.pytest_cache/",
    "**/.mypy_cache/",
    "**/.ruff_cache/",
    "**/dist/",
    "**/build/",
    "**/target/",
    "**/.next/",
    "**/.nuxt/",
    "**/coverage/",
)

DEFAULT_BROWSE_LIMIT = 100
MAX_BROWSE_LIMIT = 500
MAX_BROWSE_SCAN = 10_000
MAX_PREVIEW_LINES = 200
DEFAULT_PREVIEW_LINES = 80
MAX_LINE_BYTES = 8_192
MAX_PREVIEW_OUTPUT_BYTES = 48_000
MAX_BROWSE_OUTPUT_BYTES = 48_000
MAX_PREVIEW_READ_BYTES = 1_048_576
MAX_TAIL_WINDOW_BYTES = 2_097_152
MAX_TAIL_INDEX_BYTES = 33_554_432
MAX_TAIL_SECONDS = 5.0
TAIL_BLOCK_BYTES = 65_536
PROBE_BYTES = 8_192
MAX_GITIGNORE_BYTES = 256_000
MAX_RELATIVE_PATH_CHARS = 4_096
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_CONTEXT_LINES = 5
MAX_SEARCH_CONTEXT_LINE_CHARS = 400
MAX_SEARCH_OUTPUT_BYTES = 48_000
MAX_SEARCH_SCAN_ENTRIES = 20_000
MAX_SEARCH_SECONDS = 5.0
MAX_SEARCH_MATCH_LINE_BYTES = 512
MAX_SEARCH_MATCH_LINES_PER_FILE = MAX_SEARCH_LIMIT + 1
MAX_SEARCH_BATCH_FILES = 16
MAX_SEARCH_BATCH_ARGUMENT_CHARS = 3_000
MAX_SEARCH_BATCH_OUTPUT_BYTES = 2_000_000
MAX_SEARCH_QUERY_CHARS = 512
MAX_REFRESH_ENTRIES = 500_000
MAX_REFRESH_SECONDS = 120.0


class ProjectSetupError(ValueError):
    """The configured project root or exclusion rules are invalid."""


@dataclass
class ToolFailure(Exception):
    code: str
    message: str


@dataclass(frozen=True)
class IgnoreRules:
    spec: GitIgnoreSpec
    basedir: Path


class _SnapshotReader:
    """Small seekable reader over the bounded in-memory preview snapshot."""

    def __init__(self, content: bytearray) -> None:
        self._content = content
        self._position = 0

    def __enter__(self) -> _SnapshotReader:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = len(self._content) + offset
        else:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def read(self, size: int = -1) -> bytes:
        end = len(self._content) if size is None or size < 0 else min(len(self._content), self._position + size)
        result = bytes(self._content[self._position:end])
        self._position = end
        return result

    def readline(self, size: int = -1) -> bytes:
        if self._position >= len(self._content) or size == 0:
            return b""
        max_end = len(self._content) if size is None or size < 0 else min(len(self._content), self._position + size)
        newline = self._content.find(b"\n", self._position, max_end)
        end = newline + 1 if newline >= 0 else max_end
        result = bytes(self._content[self._position:end])
        self._position = end
        return result


class ProjectFiles:
    def __init__(self, root: Path, extra_excludes: list[str] | None = None) -> None:
        if not root.is_absolute():
            raise ProjectSetupError("--root 必须是绝对路径；服务不会依赖当前工作目录。")
        try:
            resolved_root = root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ProjectSetupError("项目根目录不存在。") from exc
        except OSError as exc:
            raise ProjectSetupError(f"无法解析项目根目录：{exc}") from exc
        if not resolved_root.is_dir():
            raise ProjectSetupError("--root 必须指向一个目录。")
        try:
            with os.scandir(resolved_root):
                pass
        except OSError as exc:
            raise ProjectSetupError(f"项目根目录不可读取：{exc}") from exc

        self.root = resolved_root
        self._ignore_cache: dict[Path, IgnoreRules] = {}
        self._extra_excludes = self._compile_extra_excludes(extra_excludes or [])
        self._default_excludes = GitIgnoreSpec.from_lines(DEFAULT_EXCLUDES)

    @staticmethod
    def _compile_extra_excludes(patterns: list[str]) -> GitIgnoreSpec:
        for pattern in patterns:
            if not pattern or pattern.startswith("!"):
                raise ProjectSetupError("--exclude 必须是非空排除规则，不能以 ! 开始。")
            if "\x00" in pattern or ":" in pattern:
                raise ProjectSetupError("--exclude 规则不能包含 NUL 或冒号。")
            normalized = pattern.replace("\\", "/")
            if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
                raise ProjectSetupError("--exclude 规则必须限制在项目根目录内。")
        try:
            return GitIgnoreSpec.from_lines(patterns)
        except Exception as exc:
            raise ProjectSetupError(f"无法解析 --exclude 规则：{exc}") from exc

    @staticmethod
    def _normalise_relative(value: str, *, allow_root: bool) -> str:
        if not isinstance(value, str):
            raise ToolFailure("invalid_path", "路径必须是字符串。")
        if "\x00" in value:
            raise ToolFailure("invalid_path", "路径不能包含 NUL 字符。")
        if value == "" and allow_root:
            return ""
        if value == "." and allow_root:
            return ""
        win_path = PureWindowsPath(value)
        if win_path.drive or win_path.root or value.startswith(("/", "\\")):
            raise ToolFailure("invalid_path", "只接受项目根目录下的相对路径。")
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if any(part == ".." for part in parts):
            raise ToolFailure("invalid_path", "路径不能包含 ..。")
        if any(":" in part for part in parts):
            raise ToolFailure("invalid_path", "路径不能包含 Windows 驱动器或 ADS 冒号。")
        if any(part == "" for part in parts):
            raise ToolFailure("invalid_path", "路径不能包含空路径段。")
        if any(part == "." for part in parts):
            raise ToolFailure("invalid_path", "路径不能包含 . 路径段。")
        if len(normalized) > MAX_RELATIVE_PATH_CHARS:
            raise ToolFailure("invalid_path", f"相对路径不能超过 {MAX_RELATIVE_PATH_CHARS} 个字符。")
        if os.name == "nt":
            reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
            for part in parts:
                if part.endswith((" ", ".")):
                    raise ToolFailure("invalid_path", "Windows 路径段不能以空格或点结尾。")
                stem = part.split(".", 1)[0].upper()
                if stem in reserved or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem):
                    raise ToolFailure("invalid_path", "Windows 设备名不能作为项目文件路径。")
        return PurePosixPath(*parts).as_posix()

    def _path_for(self, relative: str, *, allow_root: bool = False) -> tuple[str, Path]:
        normal = self._normalise_relative(relative, allow_root=allow_root)
        candidate = self.root.joinpath(*normal.split("/")) if normal else self.root

        # Never traverse symbolic links, junctions, or any other Windows reparse point.
        current = self.root
        if normal:
            for part in normal.split("/"):
                current = current / part
                try:
                    info = current.lstat()
                except FileNotFoundError as exc:
                    raise ToolFailure("not_found", f"路径不存在：{normal}") from exc
                except PermissionError as exc:
                    raise ToolFailure("unreadable", f"路径不可读取：{normal}") from exc
                except OSError as exc:
                    raise ToolFailure("unreadable", f"无法检查路径：{normal}（{exc}）") from exc
                if self._is_reparse_or_symlink(info, current):
                    raise ToolFailure("link_disallowed", f"路径包含符号链接或目录联接，已拒绝访问：{normal}")

        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("not_found", f"路径不存在：{normal or '.'}") from exc
        except PermissionError as exc:
            raise ToolFailure("unreadable", f"路径不可读取：{normal or '.'}") from exc
        except OSError as exc:
            raise ToolFailure("unreadable", f"无法解析路径：{normal or '.'}（{exc}）") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolFailure("path_escape", "解析后的路径越出项目根目录。") from exc
        canonical = resolved.relative_to(self.root).as_posix()
        if canonical == ".":
            canonical = ""
        if os.name == "nt" and normal:
            supplied_parts = normal.split("/")
            canonical_parts = canonical.split("/")
            if len(supplied_parts) != len(canonical_parts) or any(
                supplied.casefold() != actual.casefold()
                for supplied, actual in zip(supplied_parts, canonical_parts)
            ):
                raise ToolFailure(
                    "path_alias_disallowed",
                    "路径使用了 Windows 短文件名或其他名称别名；请从 browse 复制规范路径。",
                )
        return canonical, resolved

    @staticmethod
    def _is_reparse_or_symlink(info: os.stat_result, path: Path) -> bool:
        if stat.S_ISLNK(info.st_mode):
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)

    def _load_ignore_rules(self, directory: Path) -> IgnoreRules:
        cached = self._ignore_cache.get(directory)
        if cached is not None:
            return cached
        ignore_file = directory / ".gitignore"
        lines: list[str] = []
        try:
            info = ignore_file.lstat()
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            raise ToolFailure("ignore_rules_unreadable", "无法读取目录忽略规则。") from exc
        except OSError as exc:
            raise ToolFailure("ignore_rules_unreadable", f"无法检查目录忽略规则：{exc}") from exc
        else:
            if not self._is_reparse_or_symlink(info, ignore_file) and stat.S_ISREG(info.st_mode):
                try:
                    with ignore_file.open("rb") as stream:
                        content = stream.read(MAX_GITIGNORE_BYTES + 1)
                    if len(content) > MAX_GITIGNORE_BYTES:
                        raise ToolFailure(
                            "ignore_rules_too_large",
                            f"忽略规则文件超过 {MAX_GITIGNORE_BYTES} 字节上限：{self._rel_from_absolute(ignore_file)}",
                        )
                    lines = content.decode("utf-8-sig", errors="strict").splitlines()
                except UnicodeDecodeError as exc:
                    raise ToolFailure(
                        "ignore_rules_invalid_encoding",
                        f"忽略规则文件不是 UTF-8：{self._rel_from_absolute(ignore_file)}",
                    ) from exc
                except PermissionError as exc:
                    raise ToolFailure(
                        "ignore_rules_unreadable",
                        f"无法读取忽略规则文件：{self._rel_from_absolute(ignore_file)}",
                    ) from exc
                except OSError as exc:
                    raise ToolFailure(
                        "ignore_rules_unreadable",
                        f"读取忽略规则文件失败：{self._rel_from_absolute(ignore_file)}（{exc}）",
                    ) from exc
        try:
            rules = IgnoreRules(GitIgnoreSpec.from_lines(lines), directory)
        except Exception as exc:
            raise ToolFailure(
                "ignore_rules_invalid",
                f"忽略规则语法无效：{self._rel_from_absolute(ignore_file)}（{exc}）",
            ) from exc
        self._ignore_cache[directory] = rules
        return rules

    def _rel_from_absolute(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return ".gitignore"

    @staticmethod
    def _matched(spec: GitIgnoreSpec, relative: str, is_dir: bool) -> bool | None:
        candidate = f"{relative}/" if is_dir and relative else relative
        checked = spec.check_file(candidate)
        return checked.include

    def _is_ignored(self, relative: str, *, is_dir: bool) -> bool:
        if not relative:
            return False
        parts = relative.split("/")
        current = ""
        inherited: list[Path] = [self.root]
        for index, part in enumerate(parts):
            current = f"{current}/{part}" if current else part
            component_is_dir = index < len(parts) - 1 or is_dir
            state = False
            for base in inherited:
                base_relative = base.relative_to(self.root).as_posix()
                match_path = current if base == self.root else current.removeprefix(f"{base_relative}/")
                rules = self._load_ignore_rules(base)
                matched = self._matched(rules.spec, match_path, component_is_dir)
                if matched is not None:
                    state = matched
                if base == self.root:
                    matched = self._matched(self._default_excludes, current, component_is_dir)
                    if matched:
                        state = True
            # Extra excludes are independent of .gitignore negation.
            matched_extra = self._matched(self._extra_excludes, current, component_is_dir)
            if matched_extra:
                state = True
            if state and component_is_dir:
                return True
            if state:
                return True
            if component_is_dir:
                inherited.append(self.root.joinpath(*current.split("/")))
        return False

    @staticmethod
    def _failure(exc: ToolFailure) -> dict[str, Any]:
        return {"ok": False, "error": exc.code, "message": exc.message}

    def compute_version_token(
        self,
        path: str,
        project_id: str,
        secret: bytes,
        *,
        verify_twice: bool = True,
        max_seconds: float = 5.0,
    ) -> VersionResult:
        """Compute an opaque content-version token without exposing a raw digest."""
        self._ignore_cache = {}
        try:
            relative, file_path = self._path_for(path)
            info = file_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return VersionResult(None, "not_regular_file", 0)
            if self._is_ignored(relative, is_dir=False):
                return VersionResult(None, "ignored", 0)
            return hash_file_version(
                file_path, project_id, relative, secret, verify_twice=verify_twice, max_seconds=max_seconds
            )
        except ToolFailure as exc:
            return VersionResult(None, exc.code, 0)
        except OSError:
            return VersionResult(None, "unavailable", 0)

    def capture_versioned_snapshot(
        self,
        path: str,
        project_id: str,
        secret: bytes,
        *,
        max_seconds: float = 5.0,
    ) -> SnapshotResult:
        """Capture bytes for preview and mint a token for those same bytes."""
        self._ignore_cache = {}
        try:
            relative, file_path = self._path_for(path)
            info = file_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return SnapshotResult(None, VersionResult(None, "not_regular_file", 0))
            if self._is_ignored(relative, is_dir=False):
                return SnapshotResult(None, VersionResult(None, "ignored", 0))
            return read_versioned_snapshot(
                file_path, project_id, relative, secret, max_seconds=max_seconds
            )
        except ToolFailure as exc:
            return SnapshotResult(None, VersionResult(None, exc.code, 0))
        except OSError:
            return SnapshotResult(None, VersionResult(None, "unavailable", 0))

    def version_signature(self, path: str) -> tuple[int, int, int, int, int] | None:
        """Capture a cheap file identity/metadata guard around a preview read."""
        self._ignore_cache = {}
        try:
            relative, file_path = self._path_for(path)
            if self._is_ignored(relative, is_dir=False):
                return None
            return capture_file_signature(file_path)
        except ToolFailure:
            return None

    def scan_manifest(self, directory: str = "") -> tuple[str, list[IndexedEntry]]:
        """Build a complete, bounded metadata snapshot for a directory scope.

        Nothing is written to the index here. Callers can therefore commit only
        after enumeration of the entire selected scope succeeds.
        """
        self._ignore_cache = {}
        try:
            scope, scope_path = self._path_for(directory, allow_root=True)
            if not scope_path.is_dir():
                raise ToolFailure("not_directory", f"刷新范围不是目录：{scope or '.'}")
            if self._is_ignored(scope, is_dir=True):
                raise ToolFailure("ignored", f"刷新范围被项目忽略规则排除：{scope or '.'}")
            pending = [scope]
            entries: list[IndexedEntry] = []
            if scope:
                scope_info = scope_path.lstat()
                entries.append(
                    IndexedEntry(scope, "directory", None, getattr(scope_info, "st_mtime_ns", None))
                )
            started = time.monotonic()
            while pending:
                if time.monotonic() - started > MAX_REFRESH_SECONDS:
                    raise ToolFailure(
                        "refresh_timeout",
                        f"清单扫描超过 {MAX_REFRESH_SECONDS:g} 秒；旧清单未更改，请缩小刷新目录。",
                    )
                current_relative = pending.pop()
                canonical, current_path = self._path_for(current_relative, allow_root=True)
                if self._is_ignored(canonical, is_dir=True):
                    continue
                try:
                    with os.scandir(current_path) as children:
                        for child in children:
                            if time.monotonic() - started > MAX_REFRESH_SECONDS:
                                raise ToolFailure(
                                    "refresh_timeout",
                                    f"清单扫描超过 {MAX_REFRESH_SECONDS:g} 秒；旧清单未更改，请缩小刷新目录。",
                                )
                            try:
                                info = child.stat(follow_symlinks=False)
                            except OSError as exc:
                                raise ToolFailure(
                                    "refresh_scan_failed",
                                    f"检查目录项失败，旧清单未更改：{child.path}（{exc}）",
                                ) from exc
                            child_path = Path(child.path)
                            if self._is_reparse_or_symlink(info, child_path):
                                # Linked paths are outside this service's supported inventory boundary.
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                kind = "directory"
                            elif stat.S_ISREG(info.st_mode):
                                kind = "file"
                            else:
                                continue
                            relative = f"{canonical}/{child.name}" if canonical else child.name
                            if len(relative) > MAX_RELATIVE_PATH_CHARS:
                                raise ToolFailure(
                                    "invalid_path",
                                    f"相对路径超过 {MAX_RELATIVE_PATH_CHARS} 个字符，旧清单未更改：{relative[:120]}",
                                )
                            # Reject names that cannot be safely addressed through the normal tools.
                            self._normalise_relative(relative, allow_root=False)
                            if self._is_ignored(relative, is_dir=(kind == "directory")):
                                continue
                            if kind == "directory":
                                pending.append(relative)
                                entries.append(
                                    IndexedEntry(
                                        relative,
                                        "directory",
                                        None,
                                        getattr(info, "st_mtime_ns", None),
                                    )
                                )
                            else:
                                entries.append(
                                    IndexedEntry(
                                        relative,
                                        "file",
                                        int(info.st_size),
                                        getattr(info, "st_mtime_ns", None),
                                    )
                                )
                            if len(entries) > MAX_REFRESH_ENTRIES:
                                raise ToolFailure(
                                    "refresh_entry_limit",
                                    f"清单扫描超过 {MAX_REFRESH_ENTRIES} 个项目项；旧清单未更改，请缩小刷新目录。",
                                )
                except ToolFailure:
                    raise
                except PermissionError as exc:
                    raise ToolFailure(
                        "unreadable",
                        f"清单扫描目录不可读取，旧清单未更改：{canonical or '.'}",
                    ) from exc
                except OSError as exc:
                    raise ToolFailure(
                        "refresh_scan_failed",
                        f"清单扫描失败，旧清单未更改：{canonical or '.'}（{exc}）",
                    ) from exc
            return scope, entries
        except ToolFailure:
            raise
        except OSError as exc:
            raise ToolFailure("refresh_scan_failed", f"清单扫描失败，旧清单未更改：{exc}") from exc

    def browse(self, *, directory: str = "", limit: int = DEFAULT_BROWSE_LIMIT, offset: int = 0) -> dict[str, Any]:
        self._ignore_cache = {}
        if type(limit) is not int or limit < 1:
            return self._failure(ToolFailure("invalid_input", "limit 必须是正整数。"))
        if type(offset) is not int or offset < 0 or offset > MAX_BROWSE_SCAN:
            return self._failure(ToolFailure("invalid_input", f"offset 必须是 0 到 {MAX_BROWSE_SCAN} 的整数。"))
        effective_limit = min(limit, MAX_BROWSE_LIMIT)
        try:
            relative, path = self._path_for(directory, allow_root=True)
            if not path.is_dir():
                raise ToolFailure("not_directory", f"目标不是目录：{relative or '.'}")
            if self._is_ignored(relative, is_dir=True):
                raise ToolFailure("ignored", f"目录被默认或项目忽略规则排除：{relative}")
            entries: list[dict[str, str]] = []
            scanned = 0
            scan_limited = False
            try:
                with os.scandir(path) as children:
                    for entry in children:
                        if scanned >= MAX_BROWSE_SCAN:
                            scan_limited = True
                            break
                        scanned += 1
                        child_path = Path(entry.path)
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except PermissionError:
                            continue
                        if self._is_reparse_or_symlink(info, child_path):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(info.st_mode):
                            kind = "file"
                        else:
                            continue
                        child_relative = f"{relative}/{entry.name}" if relative else entry.name
                        if len(child_relative) > MAX_RELATIVE_PATH_CHARS:
                            continue
                        if self._is_ignored(child_relative, is_dir=(kind == "directory")):
                            continue
                        entries.append({"path": child_relative, "type": kind})
            except PermissionError as exc:
                raise ToolFailure("unreadable", f"目录不可读取：{relative or '.'}") from exc
            except OSError as exc:
                raise ToolFailure("unreadable", f"读取目录失败：{relative or '.'}（{exc}）") from exc

            entries.sort(key=lambda item: (item["path"].casefold(), item["path"]))
            page: list[dict[str, str]] = []
            output_limited = False
            for entry in entries[offset : offset + effective_limit]:
                candidate_page = [*page, entry]
                page_more = offset + len(candidate_page) < len(entries)
                next_value = offset + len(candidate_page) if page_more else None
                if output_limited:
                    continuation = "浏览结果达到输出字节上限；从 next_offset 继续。"
                elif next_value is not None:
                    continuation = f"再次调用 browse(directory={relative!r}, offset={next_value}, limit={effective_limit})。"
                elif scan_limited:
                    continuation = "枚举预算已用完且无法提供剩余目录项；请改为浏览更小的子目录。"
                else:
                    continuation = None
                candidate = {
                    "ok": True,
                    "directory": relative or ".",
                    "entries": candidate_page,
                    "pagination": {
                        "limit": effective_limit,
                        "offset": offset,
                        "next_offset": next_value,
                        "has_more": page_more or scan_limited,
                        "enumeration_limited": scan_limited,
                        "output_limited": output_limited,
                        "examined": scanned,
                        "continuation": continuation,
                    },
                }
                if self._json_size(candidate) > MAX_BROWSE_OUTPUT_BYTES:
                    output_limited = True
                    break
                page.append(entry)

            has_more_in_sample = offset + len(page) < len(entries)
            more = has_more_in_sample or scan_limited or output_limited
            next_value = offset + len(page) if has_more_in_sample and page else None
            if output_limited:
                continuation = "浏览结果达到输出字节上限；使用 next_offset 继续。"
            elif next_value is not None and has_more_in_sample:
                continuation = f"再次调用 browse(directory={relative!r}, offset={next_value}, limit={effective_limit})。"
            elif scan_limited:
                continuation = "枚举预算已用完且无法提供剩余目录项；请改为浏览更小的子目录。"
            else:
                continuation = None
            result = {
                "ok": True,
                "directory": relative or ".",
                "entries": page,
                "pagination": {
                    "limit": effective_limit,
                    "offset": offset,
                    "next_offset": next_value,
                    "has_more": more,
                    "enumeration_limited": scan_limited,
                    "output_limited": output_limited,
                    "examined": scanned,
                    "continuation": continuation,
                },
            }
            while page and self._json_size(result) > MAX_BROWSE_OUTPUT_BYTES:
                page.pop()
                more = offset + len(page) < len(entries) or scan_limited or output_limited
                next_value = offset + len(page) if offset + len(page) < len(entries) and page else None
                result["entries"] = page
                result["pagination"].update(
                    has_more=more,
                    next_offset=next_value,
                    output_limited=True,
                    continuation="浏览结果达到输出字节上限；使用 next_offset 继续。" if next_value is not None else "单个路径过长，无法在输出预算内返回；请浏览更小的子目录。",
                )
            if self._json_size(result) > MAX_BROWSE_OUTPUT_BYTES:
                return self._failure(ToolFailure("output_limit", "目录路径过长，无法在输出预算内返回。"))
            return result
        except ToolFailure as exc:
            return self._failure(exc)

    def _collect_search_files(self, directory: str, deadline: float) -> tuple[list[str], bool]:
        """Enumerate regular files using the same ignore and reparse-point rules as browse."""
        files: list[str] = []
        pending = [directory]
        examined = 0
        scan_limited = False

        while pending:
            if time.monotonic() >= deadline:
                raise TimeoutError
            current_relative = pending.pop()
            canonical, current_path = self._path_for(current_relative, allow_root=True)
            if self._is_ignored(canonical, is_dir=True):
                continue
            try:
                with os.scandir(current_path) as children:
                    for entry in children:
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        if examined >= MAX_SEARCH_SCAN_ENTRIES:
                            scan_limited = True
                            pending.clear()
                            break
                        examined += 1
                        child_path = Path(entry.path)
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except (FileNotFoundError, PermissionError):
                            continue
                        if self._is_reparse_or_symlink(info, child_path):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(info.st_mode):
                            kind = "file"
                        else:
                            continue
                        child_relative = f"{canonical}/{entry.name}" if canonical else entry.name
                        if len(child_relative) > MAX_RELATIVE_PATH_CHARS:
                            continue
                        if self._is_ignored(child_relative, is_dir=(kind == "directory")):
                            continue
                        try:
                            checked_relative, checked_path = self._path_for(child_relative)
                        except ToolFailure as exc:
                            # A concurrently removed or replaced entry is not followed.
                            if exc.code in {"not_found", "link_disallowed", "path_alias_disallowed"}:
                                continue
                            raise
                        if kind == "directory":
                            pending.append(checked_relative)
                        elif checked_path.is_file():
                            files.append(checked_relative)
            except TimeoutError:
                raise
            except PermissionError as exc:
                raise ToolFailure("unreadable", f"搜索目录不可读取：{canonical or '.'}") from exc
            except OSError as exc:
                raise ToolFailure("unreadable", f"枚举搜索目录失败：{canonical or '.'}（{exc}）") from exc

        files.sort(key=lambda value: (value.casefold(), value))
        return files, scan_limited

    @staticmethod
    def _search_base_result(
        *,
        mode: str,
        query: str,
        directory: str,
        limit: int,
        context_lines: int,
        results: list[dict[str, Any]],
        outcome: str,
        truncated: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        continuation = None
        if truncated or outcome == "timeout":
            continuation = "搜索结果受预算或数量上限影响；请缩小 directory/query 范围后再次搜索。"
        return {
            "ok": True,
            "mode": mode,
            "query": query,
            "directory": directory or ".",
            "limit": limit,
            "context_lines": context_lines if mode == "source" else None,
            "results": results,
            "outcome": outcome,
            "truncated": truncated,
            "reason": reason,
            "continuation": continuation,
        }

    @staticmethod
    def _short_search_text(value: str, query: str, *, case_sensitive: bool) -> tuple[str, bool]:
        if len(value) <= MAX_SEARCH_CONTEXT_LINE_CHARS:
            return value, False
        haystack = value if case_sensitive else value.casefold()
        needle = query if case_sensitive else query.casefold()
        position = haystack.find(needle)
        if position < 0:
            position = 0
        start = max(0, position - MAX_SEARCH_CONTEXT_LINE_CHARS // 3)
        end = min(len(value), start + MAX_SEARCH_CONTEXT_LINE_CHARS)
        if end - start < MAX_SEARCH_CONTEXT_LINE_CHARS:
            start = max(0, end - MAX_SEARCH_CONTEXT_LINE_CHARS)
        shortened = ("…" if start else "") + value[start:end] + ("…" if end < len(value) else "")
        return shortened, True

    def _search_hit_details(
        self,
        *,
        path: str,
        line_number: int,
        query: str,
        context_lines: int,
        case_sensitive: bool,
        deadline: float,
    ) -> tuple[dict[str, Any], bool, str | None]:
        """Build a bounded match excerpt and optional neighboring lines."""
        # Do not return ripgrep's lossy replacement output for invalid UTF-8.
        # The preview tool validates the actual hit line and supplies the excerpt.
        target_text = query
        snippet_truncated = True
        context: list[dict[str, Any]] = []
        context_truncated = False
        skip_reason: str | None = None

        if time.monotonic() < deadline:
            start_line = max(1, line_number - context_lines)
            requested_count = max(1, min(2 * context_lines + 1, MAX_PREVIEW_LINES))
            preview = self.preview(path=path, start_line=start_line, line_count=requested_count)
            if preview.get("ok"):
                for row in preview.get("lines", []):
                    row_number = row["line_number"]
                    text, was_shortened = self._short_search_text(
                        row["content"], query, case_sensitive=case_sensitive
                    )
                    if row_number == line_number:
                        target_text = row["content"]
                        snippet_truncated = was_shortened
                    elif context_lines:
                        context.append(
                            {
                                "line_number": row_number,
                                "content": text,
                                "truncated": was_shortened,
                            }
                        )
                        context_truncated = context_truncated or was_shortened
                context_truncated = context_truncated or preview.get("reason") in {
                    "line_limit",
                    "read_budget",
                    "output_budget",
                }
                if not any(row["line_number"] == line_number for row in preview.get("lines", [])):
                    # The line is beyond preview's bounded seek budget or exceeds its
                    # per-line limit. Return only the known literal query as a safe excerpt.
                    target_text = query
                    snippet_truncated = True
                    context_truncated = context_truncated or bool(context_lines)
            else:
                skip_reason = preview.get("error", "preview_failed")
        else:
            context_truncated = bool(context_lines)

        snippet, was_shortened = self._short_search_text(
            target_text, query, case_sensitive=case_sensitive
        )
        snippet_truncated = snippet_truncated or was_shortened
        return (
            {
                "path": path,
                "line_number": line_number,
                "snippet": snippet,
                "snippet_truncated": snippet_truncated,
                "context": context,
                "context_truncated": context_truncated,
            },
            snippet_truncated or context_truncated,
            skip_reason,
        )

    def search(
        self,
        *,
        mode: str,
        query: str,
        directory: str = "",
        limit: int = DEFAULT_SEARCH_LIMIT,
        context_lines: int = 0,
        case_sensitive: bool = True,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        """Search project paths or literal UTF-8 source text with bounded work and output."""
        self._ignore_cache = {}
        if not isinstance(mode, str) or mode not in {"path", "source"}:
            return self._failure(ToolFailure("invalid_input", "mode 必须是 path 或 source。"))
        if not isinstance(query, str) or not query:
            return self._failure(ToolFailure("invalid_input", "query 必须是非空字符串。"))
        if "\x00" in query or "\n" in query or "\r" in query:
            return self._failure(ToolFailure("invalid_input", "query 不能包含 NUL 或换行符。"))
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            return self._failure(
                ToolFailure("invalid_input", f"query 不能超过 {MAX_SEARCH_QUERY_CHARS} 个字符。")
            )
        try:
            query.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            return self._failure(ToolFailure("invalid_input", "query 必须是有效 Unicode 文本。"))
        if type(limit) is not int or limit < 1:
            return self._failure(ToolFailure("invalid_input", "limit 必须是正整数。"))
        if type(context_lines) is not int or not 0 <= context_lines <= MAX_SEARCH_CONTEXT_LINES:
            return self._failure(
                ToolFailure(
                    "invalid_input",
                    f"context_lines 必须是 0 到 {MAX_SEARCH_CONTEXT_LINES} 的整数。",
                )
            )
        if type(case_sensitive) is not bool:
            return self._failure(ToolFailure("invalid_input", "case_sensitive 必须是布尔值。"))
        if file_path is not None and (mode != "source" or directory):
            return self._failure(ToolFailure("invalid_input", "file_path 仅适用于源码搜索，且不能同时指定 directory。"))

        effective_limit = min(limit, MAX_SEARCH_LIMIT)
        deadline = time.monotonic() + MAX_SEARCH_SECONDS
        rg_executable: str | None = None
        if mode == "source":
            rg_executable = shutil.which("rg")
            if rg_executable is None:
                return {
                    "ok": False,
                    "error": "rg_unavailable",
                    "outcome": "rg_unavailable",
                    "message": "未找到 ripgrep（rg）；浏览与预览仍可使用，请安装 ripgrep 后重试源码搜索。",
                }

        try:
            if file_path is not None:
                directory_relative, selected_path = self._path_for(file_path)
                if selected_path.is_dir():
                    raise ToolFailure("not_file", f"搜索范围是目录，不是文件：{directory_relative}")
                if not selected_path.is_file():
                    raise ToolFailure("not_file", f"搜索范围不是普通文件：{directory_relative}")
                if self._is_ignored(directory_relative, is_dir=False):
                    raise ToolFailure("ignored", f"搜索文件被默认或项目忽略规则排除：{directory_relative}")
                files = [directory_relative]
                scan_limited = False
            else:
                directory_relative, directory_path = self._path_for(directory, allow_root=True)
                if not directory_path.is_dir():
                    raise ToolFailure("not_directory", f"搜索范围不是目录：{directory_relative or '.'}")
                if self._is_ignored(directory_relative, is_dir=True):
                    raise ToolFailure("ignored", f"搜索范围被默认或项目忽略规则排除：{directory_relative}")

                try:
                    files, scan_limited = self._collect_search_files(directory_relative, deadline)
                except TimeoutError:
                    return self._search_base_result(
                        mode=mode,
                        query=query,
                        directory=directory_relative,
                        limit=effective_limit,
                        context_lines=context_lines,
                        results=[],
                        outcome="timeout",
                        truncated=True,
                        reason="timeout",
                    )

            if mode == "path":
                needle = query if case_sensitive else query.casefold()
                matches = [
                    {"path": path, "type": "file"}
                    for path in files
                    if needle in (path if case_sensitive else path.casefold())
                ]
                result_limit_hit = len(matches) > effective_limit
                results: list[dict[str, Any]] = matches[:effective_limit]
                truncated = result_limit_hit or scan_limited
                reason = "result_limit" if result_limit_hit else ("scan_limit" if scan_limited else None)
                outcome = "truncated" if truncated else ("no_results" if not results else "complete")
                response = self._search_base_result(
                    mode=mode,
                    query=query,
                    directory=directory_relative,
                    limit=effective_limit,
                    context_lines=context_lines,
                    results=results,
                    outcome=outcome,
                    truncated=truncated,
                    reason=reason,
                )
                while response["results"] and self._json_size(response) > MAX_SEARCH_OUTPUT_BYTES:
                    response["results"].pop()
                    response.update(
                        outcome="truncated",
                        truncated=True,
                        reason="output_limit",
                        continuation="搜索结果达到输出字节上限；请缩小 directory/query 范围后再次搜索。",
                    )
                if self._json_size(response) > MAX_SEARCH_OUTPUT_BYTES:
                    return self._failure(ToolFailure("output_limit", "搜索范围路径过长，无法在输出预算内返回。"))
                return response

            if not files:
                return self._search_base_result(
                    mode=mode,
                    query=query,
                    directory=directory_relative,
                    limit=effective_limit,
                    context_lines=context_lines,
                    results=[],
                    outcome="truncated" if scan_limited else "no_results",
                    truncated=scan_limited,
                    reason="scan_limit" if scan_limited else None,
                )

            # Each process receives only literal flags and a bounded list of already-vetted files.
            # --max-columns and --max-count bound stdout to a few MiB per batch even for huge lines.
            batches: list[list[str]] = []
            current_batch: list[str] = []
            current_chars = 0
            for path in files:
                cost = len(path) + 4  # leave room for quoting/argument separators on Windows
                if current_batch and (
                    len(current_batch) >= MAX_SEARCH_BATCH_FILES
                    or current_chars + cost > MAX_SEARCH_BATCH_ARGUMENT_CHARS
                ):
                    batches.append(current_batch)
                    current_batch = []
                    current_chars = 0
                current_batch.append(path)
                current_chars += cost
            if current_batch:
                batches.append(current_batch)

            raw_hits: list[tuple[str, int, str]] = []
            seen_hits: set[tuple[str, int]] = set()
            timed_out = False
            rg_error: str | None = None
            batch_output_limited = False
            for batch in batches:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                command = [
                    rg_executable or "rg",
                    "--no-config",
                    "--color",
                    "never",
                    "--no-heading",
                    "--with-filename",
                    "--line-number",
                    "--null",
                    "--fixed-strings",
                    "--encoding",
                    "UTF-8",
                    "--max-columns",
                    str(MAX_SEARCH_MATCH_LINE_BYTES),
                    "--max-columns-preview",
                    "--max-count",
                    str(MAX_SEARCH_LIMIT + 1),
                ]
                if not case_sensitive:
                    command.append("--ignore-case")
                command.extend(["-e", query, "--", *batch])
                try:
                    returncode, output, output_limited = self._run_rg_bounded(
                        command,
                        cwd=self.root,
                        timeout=remaining,
                    )
                except subprocess.TimeoutExpired as exc:
                    output = exc.stdout or b""
                    timed_out = True
                    if isinstance(output, str):
                        output = output.encode("utf-8", errors="replace")
                    raw_hits.extend(self._parse_rg_matches(output, batch))
                    break
                except FileNotFoundError:
                    return {
                        "ok": False,
                        "error": "rg_unavailable",
                        "outcome": "rg_unavailable",
                        "message": "ripgrep（rg）在搜索期间不可用；浏览与预览仍可使用。",
                    }
                except OSError as exc:
                    rg_error = f"无法启动 ripgrep（rg）：{exc}"
                    break

                if output_limited:
                    batch_output_limited = True
                if returncode not in (0, 1) and not output_limited:
                    rg_error = f"ripgrep 搜索失败（退出代码 {returncode}）。"
                    break
                parsed = self._parse_rg_matches(output, batch)
                for hit_path, line_number, snippet in parsed:
                    key = (hit_path, line_number)
                    if key in seen_hits:
                        continue
                    seen_hits.add(key)
                    raw_hits.append((hit_path, line_number, snippet))
                    if len(raw_hits) > effective_limit:
                        break
                if len(raw_hits) > effective_limit or batch_output_limited:
                    break

            if rg_error is not None:
                return {
                    "ok": False,
                    "error": "search_failed",
                    "outcome": "search_failed",
                    "message": rg_error,
                }

            extra_hit = len(raw_hits) > effective_limit
            raw_hits = raw_hits[:effective_limit]
            results = []
            snippets_truncated = False
            context_timed_out = False
            skipped_files: list[dict[str, str]] = []
            skipped_file_count = 0
            for hit_path, line_number, _rg_snippet in raw_hits:
                hit, was_truncated, skip_reason = self._search_hit_details(
                    path=hit_path,
                    line_number=line_number,
                    query=query,
                    context_lines=context_lines,
                    case_sensitive=case_sensitive,
                    deadline=deadline,
                )
                if skip_reason is not None:
                    skipped_file_count += 1
                    if len(skipped_files) < 20:
                        skipped_files.append({"path": hit_path, "reason": skip_reason})
                    continue
                if time.monotonic() >= deadline:
                    context_timed_out = True
                candidate_results = [*results, hit]
                candidate_response = self._search_base_result(
                    mode=mode,
                    query=query,
                    directory=directory_relative,
                    limit=effective_limit,
                    context_lines=context_lines,
                    results=candidate_results,
                    outcome="complete",
                )
                if self._json_size(candidate_response) > MAX_SEARCH_OUTPUT_BYTES:
                    batch_output_limited = True
                    break
                results.append(hit)
                snippets_truncated = snippets_truncated or was_truncated

            truncated = extra_hit or scan_limited or batch_output_limited or context_timed_out
            if skipped_file_count:
                truncated = True
            if timed_out or context_timed_out:
                outcome = "timeout"
                reason = "timeout"
                truncated = True
            elif extra_hit:
                outcome = "truncated"
                reason = "result_limit"
            elif batch_output_limited:
                outcome = "truncated"
                reason = "output_limit"
            elif scan_limited:
                outcome = "truncated"
                reason = "scan_limit"
            elif skipped_file_count:
                outcome = "truncated"
                skipped_reasons = {item["reason"] for item in skipped_files}
                reason = (
                    "unsupported_encoding"
                    if "unsupported_encoding" in skipped_reasons
                    else "unreadable_or_changed_file"
                )
            else:
                outcome = "no_results" if not results else "complete"
                reason = None

            response = self._search_base_result(
                mode=mode,
                query=query,
                directory=directory_relative,
                limit=effective_limit,
                context_lines=context_lines,
                results=results,
                outcome=outcome,
                truncated=truncated,
                reason=reason,
            )
            response["snippets_truncated"] = snippets_truncated
            response["skipped_files"] = skipped_files
            response["skipped_file_count"] = skipped_file_count
            while response["results"] and self._json_size(response) > MAX_SEARCH_OUTPUT_BYTES:
                response["results"].pop()
                response.update(
                    outcome="truncated",
                    truncated=True,
                    reason="output_limit",
                    continuation="搜索结果达到输出字节上限；请缩小 directory/query 范围后再次搜索。",
                )
            if self._json_size(response) > MAX_SEARCH_OUTPUT_BYTES:
                return self._failure(ToolFailure("output_limit", "搜索结果无法在输出预算内返回。"))
            return response
        except ToolFailure as exc:
            return self._failure(exc)

    @staticmethod
    def _parse_rg_matches(output: bytes, batch: list[str]) -> list[tuple[str, int, str]]:
        allowed = {path.casefold(): path for path in batch}
        found: list[tuple[str, int, str]] = []
        # --null separates the path from the numbered matching line, so spaces and
        # other ordinary punctuation in a path cannot be mistaken for delimiters.
        pattern = re.compile(rb"(?P<path>.*?)\x00(?P<line>[0-9]+):(?P<snippet>[^\r\n]*)(?:\r?\n|$)", re.S)
        for match in pattern.finditer(output):
            try:
                returned_path = match.group("path").decode("utf-8", errors="strict").replace("\\", "/")
                line_number = int(match.group("line"))
                snippet = match.group("snippet").decode("utf-8", errors="replace")
            except (UnicodeDecodeError, ValueError):
                continue
            canonical = allowed.get(returned_path.casefold())
            if canonical is None:
                continue
            found.append((canonical, line_number, snippet))
        return found

    @staticmethod
    def _run_rg_bounded(command: list[str], *, cwd: Path, timeout: float) -> tuple[int, bytes, bool]:
        """Run ripgrep with a hard stdout cap and no captured stderr."""
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        output = bytearray()
        output_limited = threading.Event()

        def drain_stdout() -> None:
            assert process.stdout is not None
            while True:
                remaining = MAX_SEARCH_BATCH_OUTPUT_BYTES - len(output)
                chunk = process.stdout.read(min(64 * 1024, remaining + 1))
                if not chunk:
                    return
                if len(chunk) > remaining:
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    output_limited.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
                output.extend(chunk)

        reader = threading.Thread(target=drain_stdout, name="project-preview-rg-output", daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        reader.join()
        if process.stdout is not None:
            process.stdout.close()
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout, output=bytes(output))
        return process.returncode, bytes(output), output_limited.is_set()

    @staticmethod
    def _line_payload(raw: bytes, number: int) -> str:
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        encoding = "utf-8-sig" if number == 1 else "utf-8"
        return raw.decode(encoding, errors="strict")

    @staticmethod
    def _json_size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _binary_probe(probe: bytes) -> str | None:
        if probe.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
            return "unsupported_encoding"
        if b"\x00" in probe:
            return "binary"
        control_count = sum(byte < 0x20 and byte not in (0x09, 0x0A, 0x0C, 0x0D) for byte in probe)
        if probe and control_count / len(probe) > 0.01:
            return "binary"
        return None

    def preview(
        self,
        *,
        path: str,
        start_line: int = 1,
        line_count: int = DEFAULT_PREVIEW_LINES,
        _snapshot: bytearray | None = None,
    ) -> dict[str, Any]:
        self._ignore_cache = {}
        if type(start_line) is not int or start_line < 1:
            return self._failure(ToolFailure("invalid_input", "start_line 必须是从 1 开始的正整数。"))
        if type(line_count) is not int or line_count < 1:
            return self._failure(ToolFailure("invalid_input", "line_count 必须是正整数。"))
        effective_count = min(line_count, MAX_PREVIEW_LINES)
        try:
            relative, file_path = self._path_for(path)
            if file_path.is_dir():
                raise ToolFailure("not_file", f"目标是目录，不是文件：{relative}")
            if not file_path.is_file():
                raise ToolFailure("not_file", f"目标不是普通文件：{relative}")
            if self._is_ignored(relative, is_dir=False):
                raise ToolFailure("ignored", f"文件被默认或项目忽略规则排除：{relative}")
            try:
                stream_source = _SnapshotReader(_snapshot) if _snapshot is not None else file_path.open("rb")
                snapshot_size = len(_snapshot) if _snapshot is not None else None
                with stream_source as stream:
                    probe = stream.read(PROBE_BYTES)
                    binary_kind = self._binary_probe(probe)
                    if binary_kind == "binary":
                        raise ToolFailure("binary", f"文件包含二进制 NUL 字节，无法预览：{relative}")
                    try:
                        import codecs

                        decoder = codecs.getincrementaldecoder("utf-8-sig")()
                        decoder.decode(probe, final=(len(probe) < PROBE_BYTES))
                    except UnicodeDecodeError as exc:
                        if binary_kind == "unsupported_encoding":
                            message = f"文件使用 UTF-8 以外的编码：{relative}"
                        else:
                            message = f"文件不是有效 UTF-8：{relative}"
                        raise ToolFailure("unsupported_encoding", message) from exc
                    if binary_kind == "unsupported_encoding":
                        raise ToolFailure("unsupported_encoding", f"文件使用 UTF-8 以外的编码：{relative}")
                    stream.seek(0)
                    scan_budget = MAX_PREVIEW_READ_BYTES - len(probe)
                    consumed = 0
                    current_line = 1
                    result_lines: list[dict[str, Any]] = []
                    reason: str | None = None
                    detail: str | None = None
                    requested_start_reached = False
                    hit_eof = False

                    def read_line() -> tuple[bytes | None, str | None]:
                        nonlocal consumed
                        remaining = scan_budget - consumed
                        current_size = snapshot_size if snapshot_size is not None else os.fstat(stream.fileno()).st_size
                        if stream.tell() >= current_size:
                            return None, None
                        if remaining <= 0:
                            return None, "read_budget"
                        raw = stream.readline(min(MAX_LINE_BYTES + 2, remaining))
                        consumed += len(raw)
                        if not raw:
                            return None, None
                        if b"\x00" in raw:
                            raise ToolFailure("binary", f"文件包含二进制 NUL 字节：{relative}")
                        ended_by_newline = raw.endswith(b"\n")
                        current_size = snapshot_size if snapshot_size is not None else os.fstat(stream.fileno()).st_size
                        ended_at_eof = stream.tell() >= current_size
                        content = raw[:-1] if ended_by_newline else raw
                        if content.endswith(b"\r"):
                            content = content[:-1]
                        if len(content) > MAX_LINE_BYTES:
                            return None, "line_limit"
                        if not ended_by_newline and not ended_at_eof:
                            return None, "read_budget"
                        return raw, None

                    while current_line < start_line:
                        raw, stop_reason = read_line()
                        if stop_reason:
                            reason = stop_reason
                            detail = (
                                f"跳过前序内容时在第 {current_line} 行触发单行上限；无法安全定位请求行。"
                                if stop_reason == "line_limit"
                                else f"扫描预算在到达第 {start_line} 行前用尽；无法从行号继续定位。"
                            )
                            break
                        if raw is None:
                            hit_eof = True
                            break
                        try:
                            self._line_payload(raw, current_line)
                        except UnicodeDecodeError as exc:
                            raise ToolFailure(
                                "unsupported_encoding",
                                f"文件不是有效 UTF-8（第 {current_line} 行附近）：{relative}",
                            ) from exc
                        current_line += 1

                    if reason is None and current_line == start_line:
                        requested_start_reached = True
                        while len(result_lines) < effective_count:
                            raw, stop_reason = read_line()
                            if stop_reason:
                                reason = stop_reason
                                detail = (
                                    f"第 {current_line} 行超过 {MAX_LINE_BYTES} 字节上限，未返回该行部分内容，也无法从行内续读。"
                                    if stop_reason == "line_limit"
                                    else f"读取预算在第 {current_line} 行用尽，无法确认该行是否完整。"
                                )
                                break
                            if raw is None:
                                hit_eof = True
                                break
                            try:
                                content = self._line_payload(raw, current_line)
                            except UnicodeDecodeError as exc:
                                raise ToolFailure(
                                    "unsupported_encoding",
                                    f"文件不是有效 UTF-8（第 {current_line} 行附近）：{relative}",
                                ) from exc
                            candidate_lines = result_lines + [
                                {"line_number": current_line, "content": content}
                            ]
                            candidate = {
                                "ok": True,
                                "path": relative,
                                "start_line": start_line,
                                "lines": candidate_lines,
                                "truncated": False,
                                "reason": None,
                                "next_start_line": None,
                                "continuation": None,
                            }
                            if self._json_size(candidate) > MAX_PREVIEW_OUTPUT_BYTES:
                                reason = "output_budget"
                                detail = f"预览输出达到 {MAX_PREVIEW_OUTPUT_BYTES} 字节预算；从第 {current_line} 行重新预览。"
                                break
                            result_lines.append({"line_number": current_line, "content": content})
                            current_line += 1

                    if reason is None and requested_start_reached and len(result_lines) == effective_count:
                        # Bounded lookahead distinguishes an exact EOF from a page boundary.
                        raw, stop_reason = read_line()
                        if raw is not None:
                            reason = "line_count"
                            detail = f"已达到本次请求的 {effective_count} 行；从第 {current_line} 行继续。"
                        elif stop_reason == "line_limit":
                            reason = "line_count"
                            detail = f"已达到本次请求的 {effective_count} 行；从第 {current_line} 行继续。"
                        elif stop_reason == "read_budget":
                            reason = "read_budget"
                            detail = "预览内容已返回，但剩余内容无法在本次读取预算内确认；本原型没有行索引，不能安全给出续读行号。"
                        else:
                            hit_eof = True

                    if hit_eof and reason is None:
                        reason = "file_end"
                    next_line = current_line if result_lines else None
                    if reason == "output_budget":
                        next_line = current_line
                    if reason in {"line_limit", "read_budget", "file_end"}:
                        next_line = None
                    can_continue = next_line is not None and reason in {"line_count", "output_budget"}
                    continuation = None
                    if can_continue:
                        continuation = f"再次调用 preview(path={relative!r}, start_line={next_line}, line_count={effective_count})。"
                    elif reason == "line_limit":
                        continuation = "该行已超过单行上限，工具不提供按字节拆分读取；请使用其他本地查看方式读取该行。"
                    elif reason == "read_budget":
                        continuation = detail or "读取预算耗尽，无法安全地给出续读行号；请缩小文件或从较早内容开始预览。"
                    elif reason == "output_budget":
                        continuation = f"{detail} 再次调用 preview(path={relative!r}, start_line={next_line}, line_count={effective_count})。"

                    result = {
                        "ok": True,
                        "path": relative,
                        "start_line": start_line,
                        "lines": result_lines,
                        "truncated": reason not in {None, "file_end"},
                        "reason": reason,
                        "next_start_line": next_line,
                        "continuation": continuation,
                    }
                    while result_lines and self._json_size(result) > MAX_PREVIEW_OUTPUT_BYTES:
                        result_lines.pop()
                        next_line = result_lines[-1]["line_number"] + 1 if result_lines else start_line
                        reason = "output_budget"
                        continuation = (
                            f"预览输出达到 {MAX_PREVIEW_OUTPUT_BYTES} 字节预算；"
                            f"再次调用 preview(path={relative!r}, start_line={next_line}, line_count={effective_count})。"
                        )
                        result.update(
                            lines=result_lines,
                            truncated=True,
                            reason=reason,
                            next_start_line=next_line,
                            continuation=continuation,
                        )
                    if self._json_size(result) > MAX_PREVIEW_OUTPUT_BYTES:
                        return self._failure(ToolFailure("output_limit", "文件路径过长，无法在预览输出预算内返回。"))
                    return result
            except FileNotFoundError as exc:
                raise ToolFailure("not_found", f"文件不存在：{relative}") from exc
            except PermissionError as exc:
                raise ToolFailure("unreadable", f"文件不可读取：{relative}") from exc
            except IsADirectoryError as exc:
                raise ToolFailure("not_file", f"目标是目录，不是文件：{relative}") from exc
            except OSError as exc:
                raise ToolFailure("unreadable", f"读取文件失败：{relative}（{exc}）") from exc
        except ToolFailure as exc:
            return self._failure(exc)

    def preview_tail(
        self, *, path: str, line_count: int = DEFAULT_PREVIEW_LINES
    ) -> dict[str, Any]:
        """Read a bounded tail page; absolute line numbers are omitted if indexing work exceeds budget."""
        self._ignore_cache = {}
        if type(line_count) is not int or line_count < 1:
            return self._failure(ToolFailure("invalid_input", "line_count 必须是正整数。"))
        effective_count = min(line_count, MAX_PREVIEW_LINES)
        started = time.monotonic()
        try:
            relative, file_path = self._path_for(path)
            if file_path.is_dir():
                raise ToolFailure("not_file", f"目标是目录，不是文件：{relative}")
            if not file_path.is_file():
                raise ToolFailure("not_file", f"目标不是普通文件：{relative}")
            if self._is_ignored(relative, is_dir=False):
                raise ToolFailure("ignored", f"文件被默认或项目忽略规则排除：{relative}")

            with file_path.open("rb") as stream:
                probe = stream.read(PROBE_BYTES)
                binary_kind = self._binary_probe(probe)
                if binary_kind == "binary":
                    raise ToolFailure("binary", f"文件包含二进制内容，无法预览：{relative}")
                try:
                    import codecs

                    codecs.getincrementaldecoder("utf-8-sig")().decode(probe, final=False)
                except UnicodeDecodeError as exc:
                    raise ToolFailure("unsupported_encoding", f"文件不是有效 UTF-8：{relative}") from exc
                if binary_kind == "unsupported_encoding":
                    raise ToolFailure("unsupported_encoding", f"文件使用 UTF-8 以外的编码：{relative}")

                stream.seek(0, os.SEEK_END)
                file_size = stream.tell()
                if file_size == 0:
                    return {
                        "ok": True, "path": relative, "position": "tail", "file_size": 0,
                        "lines": [], "start_line": 1, "line_numbers_complete": True,
                        "truncated": False, "reason": "file_end", "continuation": None,
                    }
                stream.seek(-1, os.SEEK_END)
                ends_with_newline = stream.read(1) == b"\n"
                needed_newlines = effective_count + (1 if ends_with_newline else 0)
                position = file_size
                window_bytes = 0
                chunks: list[bytes] = []
                newline_count = 0
                window_complete = False
                while position > 0 and newline_count < needed_newlines:
                    if time.monotonic() - started >= MAX_TAIL_SECONDS:
                        break
                    remaining = MAX_TAIL_WINDOW_BYTES - window_bytes
                    if remaining <= 0:
                        break
                    take = min(TAIL_BLOCK_BYTES, remaining, position)
                    position -= take
                    stream.seek(position)
                    chunk = stream.read(take)
                    chunks.insert(0, chunk)
                    window_bytes += len(chunk)
                    newline_count += chunk.count(b"\n")
                buffer = b"".join(chunks)
                window_complete = position == 0 or newline_count >= needed_newlines
                if not buffer:
                    raise ToolFailure("read_budget", f"在预算内无法读取文件尾部：{relative}")

                pieces = buffer.split(b"\n")
                piece_starts: list[int] = []
                cursor = position
                for index, piece in enumerate(pieces):
                    piece_starts.append(cursor)
                    cursor += len(piece) + (1 if index < len(pieces) - 1 else 0)
                if position > 0 and pieces:
                    pieces = pieces[1:]
                    piece_starts = piece_starts[1:]
                if buffer.endswith(b"\n") and pieces:
                    pieces = pieces[:-1]
                    piece_starts = piece_starts[:-1]
                selected = list(zip(pieces, piece_starts))[-effective_count:]

                decoded: list[tuple[str, int]] = []
                for raw, byte_offset in selected:
                    if len(raw) > MAX_LINE_BYTES:
                        raise ToolFailure("line_limit", f"文件尾部包含超过 {MAX_LINE_BYTES} 字节的行：{relative}")
                    if b"\x00" in raw:
                        raise ToolFailure("binary", f"文件尾部包含二进制内容：{relative}")
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    try:
                        text = raw.decode("utf-8-sig" if byte_offset == 0 else "utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise ToolFailure("unsupported_encoding", f"文件尾部不是有效 UTF-8：{relative}") from exc
                    decoded.append((text, byte_offset))

                line_numbers_complete = False
                start_line: int | None = None
                if decoded and window_complete:
                    prefix_end = decoded[0][1]
                    if prefix_end <= MAX_TAIL_INDEX_BYTES and time.monotonic() - started < MAX_TAIL_SECONDS:
                        stream.seek(0)
                        remaining = prefix_end
                        preceding_newlines = 0
                        while remaining > 0 and time.monotonic() - started < MAX_TAIL_SECONDS:
                            chunk = stream.read(min(TAIL_BLOCK_BYTES, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            preceding_newlines += chunk.count(b"\n")
                        if remaining == 0:
                            start_line = preceding_newlines + 1
                            line_numbers_complete = True

                lines = [
                    {"line_number": start_line + offset if start_line is not None else None, "content": text}
                    for offset, (text, _byte_offset) in enumerate(decoded)
                ]
                result: dict[str, Any] = {
                    "ok": True, "path": relative, "position": "tail", "file_size": file_size,
                    "lines": lines, "start_line": start_line,
                    "line_numbers_complete": line_numbers_complete,
                    "truncated": not window_complete, "reason": None if window_complete else "tail_window_budget",
                    "continuation": None if window_complete else "尾部行窗口达到读取预算；当前内容可能尚未到文件头部边界。",
                }
                while lines and self._json_size(result) > MAX_PREVIEW_OUTPUT_BYTES:
                    lines.pop(0)
                    if start_line is not None:
                        start_line = lines[0]["line_number"] if lines else None
                    result["lines"] = lines
                    result["start_line"] = start_line
                    result["truncated"] = True
                    result["reason"] = "output_budget"
                    result["continuation"] = "尾部预览达到输出字节预算，已缩短显示内容。"
                if self._json_size(result) > MAX_PREVIEW_OUTPUT_BYTES:
                    return self._failure(ToolFailure("output_limit", "文件路径过长，无法在预览输出预算内返回。"))
                return result
        except ToolFailure as exc:
            return self._failure(exc)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return self._failure(ToolFailure("unreadable", f"无法读取文件尾部：{relative if 'relative' in locals() else path}（{exc}）"))
