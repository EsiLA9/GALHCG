from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import shlex
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from project_preview.index import ProjectIndex
from project_preview.filesystem import ToolFailure
from project_preview.map_ids import project_node_id


logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64_000
STATIC_FILES = {"app.css", "app.js"}


def _redact_version_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_version_tokens(item) for key, item in value.items() if key != "version_token"}
    if isinstance(value, list):
        return [_redact_version_tokens(item) for item in value]
    return value


def _safe_open_file(
    index: ProjectIndex, project_id: str, relative_path: str, editor_command: list[str] | None
) -> dict[str, Any]:
    project = index.projects.get(project_id)
    if project is None:
        return {"ok": False, "error": "unknown_project", "message": f"未配置 project_id：{project_id}"}
    try:
        canonical, disk_path = project._path_for(relative_path)
        if disk_path.is_dir() or not disk_path.is_file():
            return {"ok": False, "error": "not_file", "message": f"目标不是普通文件：{canonical}"}
        if project._is_ignored(canonical, is_dir=False):
            return {"ok": False, "error": "ignored", "message": f"文件受项目忽略规则排除：{canonical}"}
        if editor_command:
            command = [part.replace("{path}", str(disk_path)) for part in editor_command]
            if not any("{path}" in part for part in editor_command):
                command.append(str(disk_path))
            subprocess.Popen(command, cwd=str(project.root), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        elif os.name == "nt":
            os.startfile(str(disk_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(disk_path)], cwd=str(project.root),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, shell=False)
        else:
            subprocess.Popen(["xdg-open", str(disk_path)], cwd=str(project.root),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, shell=False)
        return {"ok": True, "project_id": project_id, "path": canonical}
    except ToolFailure as exc:
        return {"ok": False, "error": exc.code, "message": exc.message}
    except FileNotFoundError:
        return {"ok": False, "error": "not_found", "message": f"文件不存在：{relative_path}"}
    except OSError as exc:
        logger.warning("Unable to open project file %r: %s", relative_path, exc)
        return {"ok": False, "error": "open_failed", "message": "无法启动系统编辑器或默认文件应用。"}


def _handler_type(
    index: ProjectIndex, session_token: str, editor_command: list[str] | None
) -> type[BaseHTTPRequestHandler]:
    class LocalUIHandler(BaseHTTPRequestHandler):
        server_version = "ProjectPreviewUI/1"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s - %s", self.client_address[0], fmt % args)

        def _origin_ok(self) -> bool:
            port = self.server.server_port
            host = (self.headers.get("Host") or "").lower()
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if host not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.lower() == f"http://{host}"

        def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _read_json(self) -> dict[str, Any] | None:
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                self._send_json({"ok": False, "error": "invalid_content_type", "message": "请求必须使用 application/json。"}, 415)
                return None
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                self._send_json({"ok": False, "error": "invalid_request", "message": "Content-Length 无效。"}, 400)
                return None
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._send_json({"ok": False, "error": "request_too_large", "message": "请求超过本地界面输入上限。"}, 413)
                return None
            try:
                value = json.loads(self.rfile.read(length) if length else b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"ok": False, "error": "invalid_json", "message": "请求 JSON 格式无效。"}, 400)
                return None
            if not isinstance(value, dict):
                self._send_json({"ok": False, "error": "invalid_request", "message": "请求正文必须是 JSON 对象。"}, 400)
                return None
            return value

        def do_GET(self) -> None:
            if not self._origin_ok():
                self._send_json({"ok": False, "error": "origin_denied", "message": "只接受本地界面来源。"}, 403)
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                try:
                    page = files("project_preview").joinpath("static", "index.html").read_text(encoding="utf-8")
                except OSError:
                    self._send_json({"ok": False, "error": "ui_unavailable", "message": "前端静态资源未安装。"}, 500)
                    return
                page = page.replace("__LOCAL_UI_TOKEN__", session_token)
                self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/assets/"):
                name = parsed.path.removeprefix("/assets/")
                if name not in STATIC_FILES:
                    self._send_json({"ok": False, "error": "not_found", "message": "资源不存在。"}, 404)
                    return
                try:
                    body = files("project_preview").joinpath("static", name).read_bytes()
                except OSError:
                    self._send_json({"ok": False, "error": "ui_unavailable", "message": "前端静态资源未安装。"}, 500)
                    return
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send_bytes(body, f"{content_type}; charset=utf-8")
                return
            if parsed.path == "/api/status":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["50"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._send_json({"ok": False, "error": "invalid_input", "message": "limit 与 offset 必须是整数。"}, 400)
                    return
                result = index.status(limit=limit, offset=offset)
                if result.get("ok"):
                    for project in result.get("projects", []):
                        project["root_node_id"] = project_node_id(project["project_id"])
                self._send_json(result)
                return
            self._send_json({"ok": False, "error": "not_found", "message": "本地界面路由不存在。"}, 404)

        def do_POST(self) -> None:
            if not self._origin_ok():
                self._send_json({"ok": False, "error": "origin_denied", "message": "只接受本地界面来源。"}, 403)
                return
            supplied_token = self.headers.get("X-Local-UI-Token", "")
            if not hmac.compare_digest(supplied_token, session_token):
                self._send_json({"ok": False, "error": "session_denied", "message": "本地界面会话无效，请重新打开页面。"}, 403)
                return
            body = self._read_json()
            if body is None:
                return
            path = urlsplit(self.path).path
            try:
                result = self._dispatch(path, body)
                self._send_json(_redact_version_tokens(result))
            except Exception as exc:  # Keep internal paths and stack details out of HTTP responses.
                logger.exception("Local UI request failed: %s", path)
                self._send_json({"ok": False, "error": "internal_error", "message": "本地查询失败，请检查服务终端诊断。"}, 500)

        def _dispatch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            project_id = body.get("project_id")
            if not isinstance(project_id, str) or project_id not in index.projects:
                return {"ok": False, "error": "unknown_project", "message": "请选择一个当前已配置项目。", "project_id": project_id}
            if path == "/api/search-map":
                return index.search_map(
                    project_id, str(body.get("query", "")), limit=body.get("limit", 20),
                    offset=body.get("offset", 0), case_sensitive=False,
                    node_types=body.get("node_types", ["Concept", "Module"]),
                )
            if path == "/api/traverse":
                return index.traverse(
                    project_id, str(body.get("start_node_id", "")),
                    relations=body.get("relations", ["contains"]),
                    direction=body.get("direction", "outgoing"),
                    max_depth=body.get("max_depth", 8), node_limit=body.get("node_limit", 50),
                    edge_limit=body.get("edge_limit", 100), cursor=body.get("cursor"),
                    node_types=body.get("node_types"),
                )
            if path == "/api/context":
                result = index.context(
                    project_id, str(body.get("node_id", "")),
                    neighbor_limit=body.get("neighbor_limit", 10),
                    evidence_offset=body.get("evidence_offset", 0),
                    evidence_limit=body.get("evidence_limit", 20),
                    check_freshness=False,
                )
                return result
            if path == "/api/verify":
                return index.verify_freshness(
                    project_id, str(body.get("owner_type", "node")),
                    str(body.get("owner_id", "")),
                )
            if path == "/api/changes":
                return index.review_changes(
                    project_id,
                    directory=str(body.get("directory", "")),
                    path=str(body.get("path", "")),
                    limit=body.get("limit", 10),
                    offset=body.get("offset", 0),
                    owner_offset=body.get("owner_offset", 0),
                    owner_limit=body.get("owner_limit", 5),
                    include_unchanged=body.get("include_unchanged", False),
                )
            if path == "/api/refresh":
                return index.refresh(project_id, str(body.get("directory", "")))
            if path == "/api/list-files":
                return index.list_files(
                    project_id, str(body.get("directory", "")),
                    include_directories=bool(body.get("include_directories", False)),
                    limit=body.get("limit", 100), offset=body.get("offset", 0),
                )
            if path == "/api/preview":
                return index.preview(
                    project_id, str(body.get("path", "")),
                    start_line=body.get("start_line", 1), line_count=body.get("line_count", 80),
                )
            if path == "/api/preview-tail":
                return index.preview_tail(
                    project_id, str(body.get("path", "")), line_count=body.get("line_count", 80),
                )
            if path == "/api/search-file":
                return index.search_file(
                    project_id, str(body.get("path", "")), str(body.get("query", "")),
                    limit=body.get("limit", 50), context_lines=body.get("context_lines", 2),
                    case_sensitive=bool(body.get("case_sensitive", False)),
                )
            if path == "/api/open-file":
                return _safe_open_file(index, project_id, str(body.get("path", "")), editor_command)
            return {"ok": False, "error": "not_found", "message": "本地界面路由不存在。"}

    return LocalUIHandler


def serve_local_ui(
    index: ProjectIndex, *, port: int = 0, open_browser: bool = True,
    editor: str | None = None,
) -> None:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port 必须是 0–65535 的整数。")
    editor_value = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    editor_command = shlex.split(editor_value) if editor_value else None
    token = os.urandom(32).hex()
    handler = _handler_type(index, token, editor_command)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/"
    logger.info("Local semantic review UI is available at %s", url)
    logger.info("You can also open http://localhost:%s/", server.server_port)
    if open_browser:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            logger.warning("Could not open a browser; visit %s manually.", url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        logger.info("Stopping local semantic review UI.")
    finally:
        server.shutdown()
        server.server_close()
