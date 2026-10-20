"""Exercise a report-only loopback HTTP server with real requests."""

import gc
import http.client
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tools.backtest.preview import ReportPreview, choose_report


def report(folder: Path) -> Path:
    (folder / "assets").mkdir(parents=True)
    (folder / "report.html").write_text('<html><script src="assets/chart.js"></script></html>')
    (folder / "chart.json").write_text('{"schema_version":1}')
    (folder / "summary.json").write_text("{}")
    (folder / "assets/chart.js").write_text('console.log("chart")')
    (folder / "assets/chart.css").write_text("body { color: black; }")
    return folder / "report.html"


@contextmanager
def serving(path: Path):
    # Earlier native-engine tests can leave reference cycles. Collect them on
    # their owning thread before an HTTP worker can trigger Python's cyclic GC.
    gc.collect()
    with ReportPreview(path) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=2)


def request(server, path=None, method="GET", headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    connection.request(method, path or urlsplit(server.url).path, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_report_and_assets_load_over_http_without_exposing_other_files(tmp_path):
    path = report(tmp_path / "run")
    (path.parent / "secret.txt").write_text("not part of the report")
    with serving(path) as server:
        assert server.server_address[0] == "127.0.0.1"
        prefix = urlsplit(server.url).path.removesuffix("report.html")
        status, html, headers = request(server)
        assert status == 200 and b"assets/chart.js" in html
        assert headers["Content-Type"].startswith("text/html")
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert request(server, prefix + "assets/chart.js")[0] == 200
        assert request(server, prefix + "chart.json")[0] == 200
        assert request(server, method="HEAD")[1] == b""
        assert request(server, prefix + "secret.txt")[0] == 404
        assert request(server, "/report.html")[0] == 404
        assert request(server, prefix + "assets/")[0] == 404
        assert request(server, method="POST")[0] == 405
        assert request(server, method="DELETE")[0] == 405
        assert request(server, method="PUT")[0] == 405
        assert request(server, "/favicon.ico")[0] == 204


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "%2e%2e/outside.txt",
        "assets/../../outside.txt",
        "assets/%2e%2e/%2e%2e/outside.txt",
        "assets/%00.js",
        "assets/..%5coutside.txt",
    ],
)
def test_traversal_is_refused(tmp_path, path):
    entry = report(tmp_path / "run")
    (tmp_path / "outside.txt").write_text("outside")
    with serving(entry) as server:
        prefix = urlsplit(server.url).path.removesuffix("report.html")
        status, body, _ = request(server, prefix + path)
        assert status == 404 and b"outside" not in body


def test_symlinks_and_cross_origin_requests_are_refused(tmp_path):
    entry = report(tmp_path / "run")
    outside = tmp_path / "outside.js"
    outside.write_text("secret")
    (entry.parent / "assets/link.js").symlink_to(outside)
    (entry.parent / "assets/linked").symlink_to(tmp_path, target_is_directory=True)
    with serving(entry) as server:
        prefix = urlsplit(server.url).path.removesuffix("report.html")
        assert request(server, prefix + "assets/link.js")[0] == 404
        assert request(server, prefix + "assets/linked/outside.js")[0] == 404
        assert request(server, headers={"Host": "other.invalid"})[0] == 403
        assert request(server, headers={"Origin": "https://other.invalid"})[0] == 403
        assert (
            request(server, headers={"Origin": f"http://127.0.0.1:{server.server_port}"})[0] == 200
        )


def test_latest_and_explicit_reports_and_missing_input(tmp_path):
    strategy = tmp_path / "strategies/trend/demo"
    strategy.mkdir(parents=True)
    (strategy / "config.yaml").write_text("strategy: {name: demo}")
    old = report(strategy / "backtests/output/old")
    new = report(strategy / "backtests/output/new")
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    assert choose_report(strategy=str(strategy)) == new.resolve()
    assert choose_report(report=str(old)) == old.resolve()
    assert choose_report(report=str(old.parent)) == old.resolve()
    with pytest.raises(ValueError, match="report"):
        choose_report(report=str(tmp_path / "missing"))
    linked = tmp_path / "linked.html"
    linked.symlink_to(old)
    with pytest.raises(ValueError, match="symlink"):
        choose_report(report=str(linked))
