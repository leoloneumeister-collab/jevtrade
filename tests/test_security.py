"""Phase 4a: local web security. Real HTTP server on an ephemeral port. Written before the implementation."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from jevbot import stock_app


async def fake_asker(snap, h):
    return {
        "direction": "up",
        "direction_conf": 0.7,
        "p_up": 0.7,
        "p_down": 0.1,
        "p_flat": 0.2,
        "uptrend": 0.6,
        "overextended": 0.2,
        "vol_regime": "normal",
        "vol_conf": 0.7,
        "entry_score": 2.5,
        "entry_conf": 0.7,
        "model": "m",
        "latency_ms": 1,
    }


@pytest.fixture
def server(tmp_path):
    def start(rate_limit=(1000, 60.0)):
        srv = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            stock_app.make_handler(fake_asker, "m", str(tmp_path / "p.jsonl"), True, rate_limit=rate_limit),
        )
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        started.append(srv)
        return srv

    started = []
    yield start
    for s in started:
        s.shutdown()
        s.server_close()


def req(srv, path, headers=None, host=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host if host is not None else f"127.0.0.1:{port}", **(headers or {})}
    c.putrequest("GET", path, skip_host=True)
    for k, v in h.items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


OK = {"X-Jevbot": "1"}


def test_binds_to_loopback_only(server):
    assert server().server_address[0] == "127.0.0.1"


def test_api_requires_the_app_header(server):
    s = server()
    assert req(s, "/api/status")[0] == 403  # a plain cross-site GET cannot add a header
    assert req(s, "/api/analyze?symbol=AAPL&demo=1")[0] == 403
    assert req(s, "/api/status", OK)[0] == 200
    assert req(s, "/api/analyze?symbol=AAPL&demo=1", OK)[0] == 200


def test_csv_export_is_a_plain_link_but_still_host_checked(server):
    s = server()
    assert req(s, "/api/scoreboard.csv")[0] == 200
    assert req(s, "/api/scoreboard.csv", host="evil.example")[0] == 403


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example", "", "localhost:1"])
def test_wrong_host_header_is_rejected_everywhere(server, host):
    s = server()
    for path in ("/", "/api/status", "/app.js"):
        assert req(s, path, OK, host=host)[0] == 403  # DNS rebinding defence


def test_localhost_name_is_allowed(server):
    s = server()
    assert req(s, "/", host=f"localhost:{s.server_address[1]}")[0] == 200


def test_foreign_origin_is_rejected(server):
    s = server()
    port = s.server_address[1]
    assert req(s, "/api/status", {**OK, "Origin": "https://evil.example"})[0] == 403
    assert req(s, "/api/status", {**OK, "Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert req(s, "/api/status", {**OK, "Origin": "null"})[0] == 403


def test_security_headers_on_page(server):
    s = server()
    st, h, body = req(s, "/")
    assert st == 200 and b"Jev Stock Check" in body
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert h["Cache-Control"] == "no-store"


def test_page_has_no_inline_script_or_style(server):
    s = server()
    page = req(s, "/")[2].decode()
    assert "<script>" not in page and "<style>" not in page and "onclick=" not in page
    assert req(s, "/app.js")[0] == 200 and req(s, "/app.css")[0] == 200
    assert "javascript" in req(s, "/app.js")[1]["Content-Type"]


@pytest.mark.parametrize(
    "path", ["/static/../secret", "/../pyproject.toml", "/app.js/../../pyproject.toml", "/index.html", "/.git/config"]
)
def test_no_path_traversal_or_extra_files(server, path):
    assert req(server(), path)[0] == 404


def test_rate_limit_protects_the_wallet(server):
    s = server(rate_limit=(3, 60.0))
    codes = [req(s, "/api/analyze?symbol=AAPL&demo=1", OK)[0] for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}
    assert req(s, "/api/status", OK)[0] == 200  # cheap endpoints are not limited


def test_hostile_ticker_text_is_never_echoed_raw(server):
    s = server()
    st, _, body = req(s, "/api/scan?symbols=" + "%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E,AAPL&demo=1", OK)
    assert st == 200 and "<" not in body.decode() and "onerror=alert" not in body.decode().replace("\\", "")
    st, _, body = req(s, "/api/analyze?symbol=" + "%3Cscript%3E", OK)
    assert st == 400 and "<script>" not in body.decode()


def test_overlong_watchlist_is_refused(server):
    s = server()
    assert req(s, "/api/scan?symbols=" + ",".join(["AAPL"] * 200), OK)[0] == 400


def test_errors_are_json(server):
    st, h, body = req(server(), "/api/analyze?symbol=%24%24", OK)
    assert st == 400 and "application/json" in h["Content-Type"] and "error" in json.loads(body)
