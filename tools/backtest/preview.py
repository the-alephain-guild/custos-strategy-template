#!/usr/bin/env python3
"""Preview one exported backtest over read-only loopback HTTP."""

from __future__ import annotations

import argparse
import gc
import mimetypes
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def choose_report(*, report: str | None = None, strategy: str | None = None) -> Path:
    if report:
        entry = Path(report).expanduser()
        if not entry.exists() and not entry.is_absolute():
            entry = ROOT / entry
        if entry.is_dir():
            entry = entry / "report.html"
    elif strategy:
        from tools.backtest.run import resolve_strategy_dir

        folder = resolve_strategy_dir(strategy) / "backtests/output"
        candidates = [
            p
            for p in folder.glob("*/report.html")
            if not p.is_symlink() and not p.parent.is_symlink()
        ]
        if not candidates:
            raise ValueError("no chart report found; run make backtest without CHART=0 first")
        entry = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.parent.name))
    else:
        raise ValueError("choose a report or strategy")
    if entry.is_symlink() or entry.parent.is_symlink():
        raise ValueError("a report symlink cannot be previewed")
    if entry.name != "report.html" or not entry.is_file():
        raise ValueError("report must be an exported report.html or its directory")
    entry = entry.resolve()
    if not all((entry.parent / name).is_file() for name in ("chart.json", "summary.json")):
        raise ValueError("report is incomplete: chart.json or summary.json is missing")
    if not (entry.parent / "assets").is_dir():
        raise ValueError("report is incomplete: assets directory is missing")
    return entry


class ReportPreview(ThreadingHTTPServer):
    """Serve a selected report, never the project or a parent directory."""

    daemon_threads = True

    def __init__(self, report: Path, port: int = 0) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535 (0 chooses a free port)")
        self.report = choose_report(report=str(report))
        self.root = self.report.parent
        self.token = secrets.token_urlsafe(24)
        # Initialize MIME tables before concurrent requests use the registry.
        mimetypes.init()
        super().__init__(("127.0.0.1", port), ReportHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = f"{self.origin}/{self.token}/report.html"


class ReportHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # The URL contains a random report path. Keep it out of access logs;
        # the operator sees the one entry URL when the preview starts.
        pass

    def do_GET(self) -> None:
        self._respond(head=False)

    def do_HEAD(self) -> None:
        self._respond(head=True)

    def do_POST(self) -> None:
        self.send_error(405, "Read-only report preview")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def _respond(self, *, head: bool) -> None:
        server = cast(ReportPreview, self.server)
        if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
            self.send_error(403)
            return
        if self.headers.get("Origin") not in (None, server.origin):
            self.send_error(403)
            return
        if urlsplit(self.path).path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        prefix = f"/{server.token}/"
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
            if not path.startswith(prefix):
                raise ValueError("unknown report")
            relative = path[len(prefix) :] or "report.html"
            parts = Path(relative).parts
            if not parts or "\\" in relative or "\x00" in relative:
                raise ValueError("invalid path")
            if any(part.startswith(".") for part in parts):
                raise ValueError("hidden or parent path")
            if relative not in (
                "report.html",
                "chart.json",
                "summary.json",
            ) and not relative.startswith("assets/"):
                raise ValueError("not a report resource")
            target = server.root
            for part in parts:
                target = target / part
                if target.is_symlink():
                    raise ValueError("symlink")
            target = target.resolve()
            if not target.is_relative_to(server.root) or not target.is_file():
                raise ValueError("not a report file")
            body = target.read_bytes()
        except (OSError, ValueError, UnicodeError):
            self.send_error(404)
            return
        self.send_response(200)
        mime = mimetypes.guess_type(target.name)[0] or "text/plain"
        self.send_header(
            "Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else "")
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if not head:
            self.wfile.write(body)


def serve_report(report: Path, port: int = 0, *, open_browser: bool = False) -> int:
    """Keep one completed report available until the operator stops its preview."""
    # A completed native engine can leave cycles owned by this CLI thread;
    # collect them before an HTTP worker has a chance to trigger cyclic GC.
    gc.collect()
    try:
        with ReportPreview(report, port) as server:
            print(
                f"Preview: {server.url}\nReport: {report}\nPID: {os.getpid()}\nStop: Ctrl+C",
                flush=True,
            )
            if open_browser:
                try:
                    opened = webbrowser.open(server.url, new=2)
                except (OSError, webbrowser.Error):
                    opened = False
                if not opened:
                    print(
                        "Browser could not be opened; open the printed URL manually.",
                        file=sys.stderr,
                    )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nPreview stopped.", flush=True)
    except (OSError, ValueError) as error:
        print(f"preview failed: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--report", help="an exported report.html or its directory")
    selection.add_argument("--strategy", help="preview the strategy's most recently written report")
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 chooses a free port")
    args = parser.parse_args()
    try:
        report = choose_report(report=args.report, strategy=args.strategy)
    except (OSError, ValueError) as error:
        print(f"preview failed: {error}", file=sys.stderr)
        return 1
    return serve_report(report, args.port)


if __name__ == "__main__":
    sys.exit(main())
