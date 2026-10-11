from __future__ import annotations

import json
from pathlib import Path


class Journal:
    """One JSON line per block: snapshot, decision, action, orders, fills, account, model version."""

    def __init__(self, path: str | None):
        self.fh = None
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.fh = p.open("w")

    def write(self, row: dict) -> None:
        if self.fh:
            self.fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self.fh:
            self.fh.close()


def read(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
