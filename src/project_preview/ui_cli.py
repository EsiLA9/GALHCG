from __future__ import annotations

import logging
import sys

from project_preview.cli import _parser, create_runtime
from project_preview.ui_server import serve_local_ui


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = _parser(
        prog="project-preview-ui",
        description="启动本地项目语义审阅界面。",
    )
    parser.add_argument("--port", type=int, default=0, help="本地界面端口；默认自动选择空闲端口。")
    parser.add_argument("--no-browser", action="store_true", help="启动服务但不自动打开浏览器。")
    parser.add_argument("--editor", help="打开文件时使用的编辑器命令；可用 {path} 指定文件位置。")
    args = parser.parse_args()
    _projects, _store, index = create_runtime(args, parser)
    try:
        serve_local_ui(index, port=args.port, open_browser=not args.no_browser, editor=args.editor)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
