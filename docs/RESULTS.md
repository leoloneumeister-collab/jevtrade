# Results so far (real data, real Jev). Updated 2026-10-13.

Model: `jev-1.13.0`. Look-ahead 5 trading days. Costs 0.10% per leg. Daily bars only.

## 1. One stock at a time (`jevbot` past test, strict policy)
| Data | Days | Finding |
|---|---|---|
| GOOG 2005 to 2013 (8 stretches) | 2,000 | Average move after "lean up" +0.47% vs +0.48% on any day. "Lean down" days were followed by gains (+0.56%). A 3-line momentum rule did better. |
| AAPL 2016 to 2017 | 250 | Only 3 independent "lean up" signals: no verdict. |
| Raw P(up) ranking (GOOG) | 2,000 | Rank link +0.061, top 20% of days +0.88% vs bottom 20% +0.22%. Positive in only 5 of 8 stretches. |

## 2. Cross-stock ranking test (the fairest test)
36 large US stocks (2009 to 2015), 3 separate windows of 150 days each, 16,200 real Jev questions.

| Window | Jev rank link (IC) | Jev top-minus-bottom 20% | Momentum IC | Momentum spread |
|---|---|---|---|---|
| 2009-10 to 2010-06 | -0.007 | +0.03% | +0.002 | -0.10% |
| 2012-03 to 2012-10 | -0.012 | -0.39% | +0.023 | +0.53% |
| 2014-08 to 2015-03 | +0.052 | +0.24% | +0.020 | -0.12% |
| **Mean** | **+0.011** | **-0.04% (-0.24% after costs)** | +0.015 | +0.10% (-0.10% after costs) |

Combined z across windows (90 independent days): Jev IC +0.64, spread -0.44. Momentum IC +0.66, spread +0.49.
Interesting would be about +2. **Neither is.** The tidy rising quintile pattern in the first window did not repeat.

## Conclusion
On large US stocks with daily data, Jev as configured here (5 questions over a ~20 number snapshot) shows **no ranking
skill and no tradable edge**, and neither does the free momentum rule. The old prices may have been seen by Jev
in training, which would normally flatter it. It still did not work.

## What this does not rule out
- Different or richer inputs (news, fundamentals), other horizons, or other questions.
- Short-horizon, high-frequency settings (order book state) that Jev is designed for. Not tested on live data yet.
- Small or less efficient markets. Large caps are the hardest place to find an edge.
- The forward scoreboard (reads made today, judged later) is still the only clean test and has not run long enough.

Reproduce: import CSVs with `jevbot import-csv`, then `jevbot cross --src file --h 5 --n 150 --skip 0`.
