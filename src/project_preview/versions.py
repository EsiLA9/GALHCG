from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path


MAX_VERSION_FILE_BYTES = 64 * 1024 * 1024
MAX_VERSION_HASH_SECONDS = 5.0
VERSION_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True)
class VersionResult:
    token: str | None
    reason: str | None
    bytes_hashed: int


@dataclass(frozen=True)
class SnapshotResult:
    content: bytearray | None
    version: VersionResult


class VersionUnavailable(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
    )


def _is_reparse_or_symlink(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if hasattr(path, "is_junction") and path.is_junction():
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def capture_file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.lstat()
        if _is_reparse_or_symlink(path, info) or not stat.S_ISREG(info.st_mode):
            return None
        return (*_file_signature(info), int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))))
    except OSError:
        return None


def make_version_token(secret: bytes, project_id: str, relative_path: str, digest: bytes) -> str:
    message = b"project-preview-v1\0" + project_id.encode("ascii") + b"\0" + relative_path.encode("utf-8") + b"\0" + digest
    mac = hmac.new(secret, message, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def read_versioned_snapshot(
    path: Path,
    project_id: str,
    relative_path: str,
    secret: bytes,
    *,
    max_file_bytes: int = MAX_VERSION_FILE_BYTES,
    max_seconds: float = MAX_VERSION_HASH_SECONDS,
) -> SnapshotResult:
    """Read one bounded in-memory snapshot and mint a token for those exact bytes.

    The caller must use ``content`` itself for preview. A token is never minted
    for a separate later read of the path, which avoids binding a preview to
    bytes that were not actually returned to the caller.
    """
    started = time.monotonic()
    content = bytearray()
    try:
        path_before = path.lstat()
        if _is_reparse_or_symlink(path, path_before):
            raise VersionUnavailable("path_changed", "文件路径已变成链接，未生成版本凭据。")
        if not stat.S_ISREG(path_before.st_mode):
            raise VersionUnavailable("not_regular_file", "只有普通文件可以生成版本凭据。")
        if path_before.st_size > max_file_bytes:
            return SnapshotResult(None, VersionResult(None, "file_too_large", 0))

        digest = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or _file_signature(before) != _file_signature(path_before):
                raise VersionUnavailable("file_changed", "读取预览快照前文件状态已变化。")
            while True:
                if time.monotonic() - started > max_seconds:
                    raise VersionUnavailable("hash_timeout", "读取预览快照超过时间预算，未生成版本凭据。")
                chunk = stream.read(VERSION_CHUNK_BYTES)
                if not chunk:
                    break
                content.extend(chunk)
                digest.update(chunk)
                if len(content) > max_file_bytes:
                    raise VersionUnavailable("file_too_large", "文件超过预览快照大小预算，未生成版本凭据。")

            after = os.fstat(stream.fileno())
            path_after = path.lstat()
            if _is_reparse_or_symlink(path, path_after):
                raise VersionUnavailable("path_changed", "读取预览快照期间文件路径变成链接，未生成版本凭据。")
            before_ctime = int(getattr(path_before, "st_ctime_ns", int(path_before.st_ctime * 1_000_000_000)))
            after_ctime = int(getattr(path_after, "st_ctime_ns", int(path_after.st_ctime * 1_000_000_000)))
            if (
                len(content) != before.st_size
                or _file_signature(after) != _file_signature(before)
                or _file_signature(path_after) != _file_signature(path_before)
                or before_ctime != after_ctime
            ):
                raise VersionUnavailable("file_changed", "读取预览快照期间文件内容或路径发生变化。")
            if time.monotonic() - started > max_seconds:
                raise VersionUnavailable("hash_timeout", "读取预览快照超过时间预算，未生成版本凭据。")
            token = make_version_token(secret, project_id, relative_path, digest.digest())
            return SnapshotResult(content, VersionResult(token, None, len(content)))
    except VersionUnavailable as exc:
        return SnapshotResult(None, VersionResult(None, exc.reason, len(content)))
    except OSError:
        return SnapshotResult(None, VersionResult(None, "unavailable", len(content)))


def hash_file_version(
    path: Path,
    project_id: str,
    relative_path: str,
    secret: bytes,
    *,
    max_file_bytes: int = MAX_VERSION_FILE_BYTES,
    max_seconds: float = MAX_VERSION_HASH_SECONDS,
    verify_twice: bool = True,
) -> VersionResult:
    """Return an opaque HMAC token for stable file bytes, or a bounded unavailable reason."""
    started = time.monotonic()
    bytes_hashed = 0
    try:
        path_before = path.lstat()
        if _is_reparse_or_symlink(path, path_before):
            raise VersionUnavailable("path_changed", "文件路径已变成链接，未生成版本凭据。")
        if not stat.S_ISREG(path_before.st_mode):
            raise VersionUnavailable("not_regular_file", "只有普通文件可以生成版本凭据。")
        if path_before.st_size > max_file_bytes:
            return VersionResult(None, "file_too_large", 0)

        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or _file_signature(before) != _file_signature(path_before):
                raise VersionUnavailable("file_changed", "计算版本凭据前文件状态已变化。")

            def digest_pass() -> tuple[bytes, int, os.stat_result]:
                nonlocal bytes_hashed
                stream.seek(0)
                digest = hashlib.sha256()
                count = 0
                while True:
                    if time.monotonic() - started > max_seconds:
                        raise VersionUnavailable("hash_timeout", "文件哈希超过时间预算，未生成版本凭据。")
                    chunk = stream.read(VERSION_CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    bytes_hashed += len(chunk)
                    if count > max_file_bytes:
                        raise VersionUnavailable("file_too_large", "文件超过哈希大小预算，未生成版本凭据。")
                    digest.update(chunk)
                return digest.digest(), count, os.fstat(stream.fileno())

            first_digest, first_count, first_after = digest_pass()
            second_digest = first_digest
            second_count = first_count
            second_after = first_after
            if verify_twice:
                second_digest, second_count, second_after = digest_pass()
            path_after = path.lstat()
            if _is_reparse_or_symlink(path, path_after):
                raise VersionUnavailable("path_changed", "哈希期间文件路径变成链接，未生成版本凭据。")
            signature = _file_signature(before)
            if (
                first_count != before.st_size
                or second_count != before.st_size
                or first_digest != second_digest
                or _file_signature(first_after) != signature
                or _file_signature(second_after) != signature
                or _file_signature(path_after) != signature
            ):
                raise VersionUnavailable("file_changed", "哈希期间文件内容或路径发生变化，未生成版本凭据。")
            if time.monotonic() - started > max_seconds:
                raise VersionUnavailable("hash_timeout", "文件哈希超过时间预算，未生成版本凭据。")
            return VersionResult(
                make_version_token(secret, project_id, relative_path, first_digest), None, bytes_hashed
            )
    except VersionUnavailable as exc:
        return VersionResult(None, exc.reason, bytes_hashed)
    except (OSError, PermissionError) as exc:
        return VersionResult(None, "unavailable", bytes_hashed)
