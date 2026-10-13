"""Real-data source: load price CSVs from any common download. Written before the implementation."""

import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from jevbot import market_data as md
from jevbot import stock_app

YAHOO = "Date,Open,High,Low,Close,Adj Close,Volume\n"
PLOTLY = "Date,AAPL.Open,AAPL.High,AAPL.Low,AAPL.Close,AAPL.Volume,AAPL.Adjusted,dn,mavg,up,direction\n"
STOOQ = "Date,Open,High,Low,Close,Volume\n"
BT = ",Open,High,Low,Close,Volume\n"


def days(n, start="2020-01-01"):
    from datetime import date, timedelta

    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def write(tmp_path, name, header, rows):
    p = tmp_path / name
    p.write_text(header + "\n".join(rows) + "\n")
    return p


def price(i):
    return 100 + i * 0.1


def test_loads_yahoo_format_and_uses_adjusted_prices(tmp_path):
    rows = [
        f"{d},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},{price(i) / 2},1000" for i, d in enumerate(days(120))
    ]
    bars = md.load_csv(write(tmp_path, "AAPL.csv", YAHOO, rows))
    assert len(bars) == 120 and bars[0].c == pytest.approx(price(0) / 2)  # adjusted close is used
    assert bars[0].h == pytest.approx((price(0) + 1) / 2)  # and O/H/L are scaled to match
    assert bars[0].d == "2020-01-01"


def test_loads_plotly_format(tmp_path):
    rows = [
        f"{d},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},5000,{price(i)},1,1,1,Up"
        for i, d in enumerate(days(100))
    ]
    bars = md.load_csv(write(tmp_path, "AAPL_2015.csv", PLOTLY, rows))
    assert len(bars) == 100 and bars[5].v == 5000


def test_loads_stooq_and_backtesting_formats(tmp_path):
    rows = [f"{d},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},900" for i, d in enumerate(days(100))]
    assert len(md.load_csv(write(tmp_path, "a.csv", STOOQ, rows))) == 100
    assert len(md.load_csv(write(tmp_path, "b.csv", BT, rows))) == 100


def test_descending_files_and_us_dates_are_normalised(tmp_path):
    ds = days(100)
    rows = [
        f"{d[5:7]}/{d[8:10]}/{d[:4]},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},1" for i, d in enumerate(ds)
    ]
    bars = md.load_csv(write(tmp_path, "c.csv", STOOQ, list(reversed(rows))))  # newest first, m/d/Y
    assert [b.d for b in bars] == ds


def test_blank_and_null_rows_are_skipped(tmp_path):
    rows = [f"{d},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},1" for i, d in enumerate(days(100))]
    rows[10] = f"{days(100)[10]},null,null,null,null,null"
    rows[11] = f"{days(100)[11]},,,,,"
    assert len(md.load_csv(write(tmp_path, "d.csv", STOOQ, rows))) == 98


@pytest.mark.parametrize(
    "body,msg",
    [
        ("Foo,Bar\n1,2\n", "columns"),
        ("Date,Open,High,Low,Close,Volume\n", "at least"),
    ],
)
def test_bad_files_give_a_clear_error(tmp_path, body, msg):
    p = tmp_path / "bad.csv"
    p.write_text(body)
    with pytest.raises(ValueError, match=msg):
        md.load_csv(p)


def test_file_symbols_and_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVBOT_DATA_DIR", str(tmp_path))
    rows = [f"{d},1,2,1,1.5,1" for d in days(100)]
    write(tmp_path, "AAPL_2015-2017.csv", STOOQ, rows)
    write(tmp_path, "goog.csv", STOOQ, rows)
    (tmp_path / "notes.txt").write_text("x")
    assert md.file_symbols() == ["AAPL", "GOOG"]
    assert md.resolve_file("aapl").name == "AAPL_2015-2017.csv"
    with pytest.raises(ValueError, match="No CSV"):
        md.resolve_file("MSFT")


def test_import_writes_a_canonical_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVBOT_DATA_DIR", str(tmp_path / "data"))
    rows = [
        f"{d},{price(i)},{price(i) + 1},{price(i) - 1},{price(i)},{price(i) / 2},1000" for i, d in enumerate(days(120))
    ]
    src = write(tmp_path, "download.csv", YAHOO, rows)
    out = md.import_csv(src, "aapl")
    assert out.name == "AAPL.csv" and md.load_csv(out)[0].c == pytest.approx(price(0) / 2)
    assert out.read_text().splitlines()[0] == "Date,Open,High,Low,Close,Volume"
    with pytest.raises(ValueError):
        md.import_csv(src, "../evil")


# ---- wired into the app ------------------------------------------------------------------
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
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("JEVBOT_DATA_DIR", str(d))
    rows = [
        f"{x},{price(i) + (i % 7)},{price(i) + 8},{price(i) - 1},{price(i) + (i % 5)},1000"
        for i, x in enumerate(days(400))
    ]
    write(d, "AAPL.csv", STOOQ, rows)
    return d


def test_analyze_from_file_is_never_logged_and_says_so(tmp_path, data_dir):
    log = str(tmp_path / "p.jsonl")
    d = asyncio.run(stock_app.analyze(fake_asker, "AAPL", 5, "file", log))
    assert d["source"] == "file" and d["src"] == "file" and d["logged"] is False and not (tmp_path / "p.jsonl").exists()
    assert d["warning"] is None  # a historical file is not "stale data"


def test_backtest_runs_on_file_data(data_dir):
    r = asyncio.run(stock_app.backtest(fake_asker, "AAPL", 5, 120, "file"))
    assert r["stats"]["days"] == 120 and r["src"] == "file"


def test_missing_file_is_a_clear_error(data_dir):
    with pytest.raises(ValueError, match="No CSV"):
        asyncio.run(stock_app.analyze(fake_asker, "MSFT", 5, "file", None))


def test_http_files_endpoint_and_src_param(tmp_path, data_dir):
    import http.client

    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), stock_app.make_handler(fake_asker, "m", str(tmp_path / "p.jsonl"), True)
    )
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        port = srv.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path, headers={"X-Jevbot": "1"})
            r = c.getresponse()
            return r.status, json.loads(r.read())

        assert get("/api/files") == (200, {"symbols": ["AAPL"]})
        st, d = get("/api/analyze?symbol=AAPL&src=file")
        assert st == 200 and d["src"] == "file"
        assert get("/api/analyze?symbol=AAPL&src=bogus")[0] == 400
        assert get("/api/analyze?symbol=MSFT&src=file")[0] == 400
        assert get("/api/analyze?symbol=AAPL&demo=1")[1]["src"] == "demo"  # old parameter still works
    finally:
        srv.shutdown()
        srv.server_close()


def test_backtest_skip_moves_the_window_back_in_time(data_dir):
    recent = asyncio.run(stock_app.backtest(fake_asker, "AAPL", 5, 60, "file"))
    older = asyncio.run(stock_app.backtest(fake_asker, "AAPL", 5, 60, "file", skip=100))
    assert (
        older["rows"][-1]["asof"] < recent["rows"][0]["asof"] or older["rows"][-1]["asof"] < recent["rows"][-1]["asof"]
    )
    assert older["stats"]["days"] == 60 and older["skip"] == 100


def test_backtest_skip_beyond_history_is_a_clear_error(data_dir):
    with pytest.raises(ValueError, match="history"):
        asyncio.run(stock_app.backtest(fake_asker, "AAPL", 5, 60, "file", skip=10_000))
