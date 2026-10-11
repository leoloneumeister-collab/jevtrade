import pytest

from jevbot.config import Config
from jevbot.feed import Tick


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.run.kill_file = str(tmp_path / "KILL")
    c.run.warmup_blocks = 5
    return c


def mk_tick(block=0, bid=3.4000, ask=3.4004, trades=(), ts=None):
    t = 0.0001
    bids = tuple((round(bid - i * t, 4), 50.0) for i in range(3))
    asks = tuple((round(ask + i * t, 4), 50.0) for i in range(3))
    return Tick(block, block * 0.3 if ts is None else ts, bids, asks, tuple(trades), None)
