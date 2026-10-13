"""Regression: a deleted constant broke the blank-model path. Found by the linter, not by the earlier tests."""

import pytest

from jevbot import stock_app
from jevbot.config import load_config


@pytest.mark.parametrize("blank", ["", "jev-latest"])
def test_blank_or_floating_model_falls_back_to_the_pinned_default(blank):
    assert stock_app.resolve_model(blank) == stock_app.DEFAULT_MODEL


def test_explicit_model_is_kept():
    assert stock_app.resolve_model("jev-9.9.9") == "jev-9.9.9"


def test_default_model_matches_shipped_config():
    assert load_config("config.toml").jev.model == stock_app.DEFAULT_MODEL


def test_scan_command_works_with_a_blank_model(monkeypatch, tmp_path, capsys):
    from jevbot.cli import main

    seen = {}

    def fake_make_asker(model):
        seen["model"] = model

        async def asker(snap, h):
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
                "model": model,
                "latency_ms": 1,
            }

        return asker

    monkeypatch.setattr(stock_app, "make_asker", fake_make_asker)
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    blank = tmp_path / "blank.toml"
    blank.write_text('[jev]\nmodel = ""\n')
    monkeypatch.setattr(
        "sys.argv",
        ["jevbot", "--config", str(blank), "scan", "--symbols", "AAPL", "--demo", "--log", str(tmp_path / "p.jsonl")],
    )
    main()
    assert seen["model"] == stock_app.DEFAULT_MODEL and "AAPL" in capsys.readouterr().out
