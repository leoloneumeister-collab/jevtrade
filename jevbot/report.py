"""Step 8 tooling: calibration, direction accuracy and the fee check, from the journal alone."""
from __future__ import annotations

import math

from .journal import read


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def calibration(rows: list[dict], horizon: int = 10, move_bps: float = 3.0, bins: int = 5) -> dict:
    """Outcome labels use only prices AFTER the decision, computed after the fact."""
    mids = [r["snap"]["mid"] for r in rows]
    tox, dirs = [], []
    for i, r in enumerate(rows):
        d = r["decision"]
        if not d or d["source"] != "jev" or i + horizon >= len(rows):
            continue
        fwd = (mids[i + horizon] / mids[i] - 1) * 1e4
        flow_sign = _sign(r["snap"]["aggressive_buy_ratio"] - 0.5)
        tox.append((d["toxic"], 1 if flow_sign * fwd >= move_bps else 0))
        actual = "up" if fwd >= move_bps else "down" if fwd <= -move_bps else "neutral"
        dirs.append((d["direction_conf"], d["direction"] == actual))

    def curve(pairs):
        out = []
        for b in range(bins):
            lo, hi = b / bins, (b + 1) / bins
            sel = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
            if sel:
                out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                            "predicted": round(sum(p for p, _ in sel) / len(sel), 3),
                            "observed": round(sum(y for _, y in sel) / len(sel), 3)})
        return out

    brier = sum((p - y) ** 2 for p, y in tox) / len(tox) if tox else None
    base = sum(y for _, y in tox) / len(tox) if tox else None
    return {"toxic_reliability": curve(tox), "toxic_brier": brier,
            "toxic_base_rate": base, "toxic_brier_baseline": base * (1 - base) if base is not None else None,
            "direction_reliability": curve(dirs), "n_judged": len(tox)}


def fee_check(summary: dict) -> str:
    if not summary["fills"]:
        return "no fills: nothing to judge"
    edge = summary["gross_pnl"] / summary["fills"]
    fee = summary["fees"] / summary["fills"]
    verdict = "OK" if edge > 0 and summary["net_pnl"] > 0 else "FEES EAT THE EDGE"
    return f"gross edge/fill {edge:.5f} vs fees/fill {fee:.5f} -> {verdict}"


def print_report(path: str) -> None:
    rows = read(path)
    models = {r["model"] for r in rows if r["model"]}
    status: dict[str, int] = {}
    for r in rows:
        status[r["status"]] = status.get(r["status"], 0) + 1
    print(f"blocks={len(rows)} models_seen={sorted(models) or 'none'} status={status}")
    cal = calibration(rows)
    if not cal["n_judged"]:
        print("no Jev decisions with a future window: nothing to calibrate")
        return
    print(f"\ntoxic_flow reliability (n={cal['n_judged']}, base rate {cal['toxic_base_rate']:.3f}):")
    for b in cal["toxic_reliability"]:
        print(f"  {b['bin']}  n={b['n']:<6} said {b['predicted']:.2f}  happened {b['observed']:.2f}")
    print(f"  brier {cal['toxic_brier']:.4f} vs constant-guess {cal['toxic_brier_baseline']:.4f} (lower than baseline = useful)")
    print("\ndirection hit-rate by confidence:")
    for b in cal["direction_reliability"]:
        print(f"  {b['bin']}  n={b['n']:<6} said {b['predicted']:.2f}  right {b['observed']:.2f}")
