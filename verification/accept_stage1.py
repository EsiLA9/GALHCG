"""Independent stdio MCP acceptance; run using the installed project's Python."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading


class Client:
    def __init__(self, command: list[str], cwd: Path):
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, text=True, encoding="utf-8", bufsize=1,
        )
        self.sequence = 0

        def receive():
            assert self.process.stdout
            for line in self.process.stdout:
                self.messages.put(line)
            self.messages.put(None)

        threading.Thread(target=receive, daemon=True).start()

    def send(self, message: dict):
        assert self.process.stdin
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict):
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        for _ in range(50):
            line = self.messages.get(timeout=20)
            if line is None:
                self.stderr.seek(0)
                raise AssertionError("Server exited: " + self.stderr.read().decode("utf-8", "replace"))
            message = json.loads(line)  # Non-protocol stdout is an acceptance failure.
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message["result"]
        raise AssertionError("No matching response")

    def call(self, name: str, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if "structuredContent" in result:
            return result["structuredContent"]
        for item in result.get("content", []):
            if item.get("type") == "text":
                return json.loads(item["text"])
        raise AssertionError(result)

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.stderr.close()


def snapshot(root: Path):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="project_preview")
    args = parser.parse_args()
    passed = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="galhcg-accept-") as temporary:
        outer = Path(temporary)
        root = outer / "中文项目"
        root.mkdir()
        (root / "src").mkdir()
        (root / "src" / "入口.py").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
        (root / "empty.txt").write_bytes(b"")
        (root / "bom.txt").write_bytes(b"\xef\xbb\xbfhello\n")
        (root / "binary.bin").write_bytes(b"hello\x00world")
        (root / "bad.txt").write_bytes(b"\xff\xfe\x80")
        (root / "long.txt").write_text("x" * 20000 + "\nlast\n", encoding="utf-8")
        (root / "large.txt").write_bytes((b"abcdefghij\n") * 200000)
        (root / "output-budget.txt").write_text(("字" * 2300 + "\n") * 20, encoding="utf-8")
        (root / "many").mkdir()
        for i in range(105):
            (root / "many" / f"{i:03}.txt").write_text(str(i), encoding="utf-8")
        (root / "wide").mkdir()
        for i in range(260):
            (root / "wide" / (f"{i:03}-" + "长" * 80 + ".txt")).write_text("x", encoding="utf-8")
        (root / ".gitignore").write_text("*.secret\n!visible.secret\nblocked/\n", encoding="utf-8")
        (root / "hidden.secret").write_text("hidden", encoding="utf-8")
        (root / "visible.secret").write_text("visible", encoding="utf-8")
        (root / "blocked").mkdir()
        (root / "blocked" / "inside.txt").write_text("hidden", encoding="utf-8")
        (root / "src" / ".gitignore").write_text("*.tmp\n!keep.tmp\n", encoding="utf-8")
        (root / "src" / "hide.tmp").write_text("hide", encoding="utf-8")
        (root / "src" / "keep.tmp").write_text("keep", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.txt").write_text("dependency", encoding="utf-8")
        (outer / "outside.txt").write_text("outside", encoding="utf-8")
        links = []
        for name, destination, is_directory in [
            ("outside-link.txt", outer / "outside.txt", False),
            ("loop", root, True),
        ]:
            try:
                (root / name).symlink_to(destination, target_is_directory=is_directory)
                links.append(name)
            except OSError as exc:
                skipped.append(f"Real symbolic link {name}: {exc}")
        before = snapshot(root)
        client = Client(
            [sys.executable, "-m", args.module, "--root", str(root), "--data-dir", str(outer / "service-data")],
            outer,
        )
        try:
            init = client.request("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "independent-stage1-acceptance", "version": "1.0"},
            })
            assert init.get("serverInfo"), init
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            names = {t["name"] for t in client.request("tools/list", {})["tools"]}
            assert {"browse", "preview", "search"} <= names, names
            passed.append("MCP handshake and browsing/search tools from unrelated cwd")

            listing = client.call("browse", directory="src")
            assert listing["ok"], listing
            assert "src/入口.py" in {e["path"] for e in listing["entries"]}, listing
            result = client.call("preview", path="src/入口.py", start_line=2, line_count=1)
            assert result["ok"] and result["lines"] == [{"line_number": 2, "content": "第二行"}], result
            passed.append("Browse to Chinese path and exact numbered line preview")

            path_search = client.call("search", mode="path", query="入口.py", directory="src")
            assert path_search["outcome"] == "complete", path_search
            assert [item["path"] for item in path_search["results"]] == ["src/入口.py"], path_search
            source_search = client.call("search", mode="source", query="第二行", directory="src")
            assert source_search["outcome"] == "complete", source_search
            assert source_search["results"][0]["path"] == "src/入口.py", source_search
            assert source_search["results"][0]["line_number"] == 2, source_search
            passed.append("Path and Chinese source search return preview-ready paths and line numbers")

            first = client.call("browse", directory="many", limit=100)
            second = client.call("browse", directory="many", limit=100, offset=first["pagination"]["next_offset"])
            assert len(first["entries"]) == 100 and len(second["entries"]) == 5, (first, second)
            paths = [e["path"] for e in first["entries"] + second["entries"]]
            assert len(set(paths)) == 105 and paths == sorted(paths), paths
            passed.append("Stable directory pagination without duplicate or missing entries")
            offset = 0
            wide_paths = []
            for _ in range(10):
                page = client.call("browse", directory="wide", limit=500, offset=offset)
                size = len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                assert size <= 48000, size
                wide_paths.extend(e["path"] for e in page["entries"])
                following = page["pagination"]["next_offset"]
                if following is None:
                    break
                assert following > offset, page
                offset = following
            assert len(wide_paths) == len(set(wide_paths)) == 260, len(wide_paths)
            limited = client.call("preview", path="output-budget.txt", line_count=200)
            size = len(json.dumps(limited, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            assert size <= 48000 and limited["reason"] == "output_budget", limited
            assert limited["next_start_line"] == len(limited["lines"]) + 1, limited
            passed.append("Actual UTF-8 result byte budgets and complete bounded directory pagination")

            ignored_paths = ["hidden.secret", "blocked/inside.txt", "src/hide.tmp", "node_modules/dep.txt"]
            if os.name == "nt":
                ignored_paths.extend(["HIDDEN.SECRET", "hidden.secret.", "hidden.secret "])
            for path in ignored_paths:
                result = client.call("preview", path=path)
                assert result.get("ok") is False, (path, result)
            for path in ["visible.secret", "src/keep.tmp"]:
                result = client.call("preview", path=path)
                assert result.get("ok") is True, (path, result)
            original_rules = (root / ".gitignore").read_bytes()
            try:
                (root / ".gitignore").write_bytes(original_rules + b"bom.txt\n")
                changed = client.call("preview", path="bom.txt")
                assert changed.get("ok") is False and changed.get("error") == "ignored", changed
            finally:
                (root / ".gitignore").write_bytes(original_rules)
            passed.append("Root/nested ignore rules, negation and direct access enforcement")

            for path in ["../outside.txt", str(outer / "outside.txt"), "src/入口.py:stream", "missing.txt", "binary.bin", "bad.txt"]:
                result = client.call("preview", path=path)
                assert result.get("ok") is False, (path, result)
            passed.append("Outside paths, ADS, missing file, binary and encoding errors")
            for name in links:
                if name == "loop":
                    result = client.call("browse", directory=name)
                else:
                    result = client.call("preview", path=name)
                assert result.get("ok") is False, (name, result)
            if links:
                passed.append("Real outside-file and cyclic-directory symbolic links rejected")

            empty = client.call("preview", path="empty.txt")
            assert empty["ok"] and empty["lines"] == [], empty
            assert empty.get("next_start_line") is None, empty
            bom = client.call("preview", path="bom.txt")
            assert bom["ok"] and bom["lines"][0]["content"] == "hello", bom
            assert bom.get("next_start_line") is None, bom
            long = client.call("preview", path="long.txt")
            assert len(json.dumps(long)) < 60000 and long.get("truncated"), long
            large = client.call("preview", path="large.txt", start_line=190000)
            assert len(json.dumps(large)) < 60000, large
            assert large.get("ok") is False or large.get("truncated"), large
            boundary = client.call("preview", path="large.txt", start_line=94580, line_count=10)
            assert all(line["content"] == "abcdefghij" for line in boundary.get("lines", [])), boundary
            passed.append("Empty/BOM files, long-line truncation and bounded deep-file scan")

            assert snapshot(root) == before, "Read-only acceptance fixture changed"
            passed.append("Target file inventory and SHA-256 content unchanged")
            if os.name == "nt":
                outside_dir = outer / "external-directory"
                outside_dir.mkdir()
                (outside_dir / "external.txt").write_text("external", encoding="utf-8")
                for name, target in [("external-junction", outside_dir), ("cycle-junction", root)]:
                    link = root / name
                    environment = dict(os.environ, ACCEPT_LINK=str(link), ACCEPT_TARGET=str(target))
                    created = subprocess.run(
                        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                         "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:ACCEPT_LINK -Target $env:ACCEPT_TARGET | Out-Null"],
                        env=environment, capture_output=True, timeout=20,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if created.returncode:
                        skipped.append(f"Real junction {name}: creation failed")
                        continue
                    try:
                        result = client.call("browse", directory=name)
                        assert result.get("ok") is False, (name, result)
                        if name == "external-junction":
                            result = client.call("preview", path=name + "/external.txt")
                            assert result.get("ok") is False, result
                    finally:
                        # Remove only this freshly created junction, never its target.
                        assert link.is_junction() and link.parent == root
                        os.rmdir(link)
                    passed.append(f"Real Windows {name} rejected")
        finally:
            client.close()
    print(json.dumps({"passed": len(passed), "checks": passed, "skipped": skipped}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
