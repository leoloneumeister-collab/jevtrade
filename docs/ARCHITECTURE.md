# Architecture

Two apps share one rule: **math in code, judgment in Jev, final say in strict rules.**

## 1. Stock website (`jevbot web`, `jevbot scan`)
```
market_data.py   free daily bars (Yahoo, then Stooq) -> retry/backoff -> validate -> per-symbol 15 min cache
stock_features.py  plain arithmetic on PAST bars only (returns, volatility, RSI, SMA distance, 52w range)
stock_jev.py     5 atomic questions in ONE call -> typed answers -> cautious LEAN UP / LEAN DOWN / SIT OUT rules
stock_app.py     analyze, backtest (Jev vs 3-line momentum rule vs holding), scan, forward scoreboard, HTTP server
static/          index.html + app.js + app.css (no inline script or style, strict CSP)
```
- **Look-ahead safety.** Features for day *i* use `bars[: i + 1]` only. A test mutates the future and asserts the
  past features do not change.
- **Honest statistics.** Signals closer than the horizon share most of their outcome window, so hit rate, average
  move and the z score use only non-overlapping signals. Fewer than 10 independent signals means no verdict.
  A result must beat plain holding, beat the momentum rule, and survive costs before it is called promising.
- **Forward scoreboard.** Every real read is appended once per (ticker, day, horizon) to `runs/predictions.jsonl`
  and resolved after the horizon passes. This is the only test that old training data cannot contaminate.
- **Local security.** Bound to 127.0.0.1. Host and Origin checks stop DNS rebinding and cross-site pages. API calls
  need an `X-Jevbot` header, so a plain cross-site request cannot trigger paid Jev calls. Rate limited. Strict CSP,
  all server text escaped before it touches the DOM. The API key is read from the environment by the server and
  never reaches the browser.

## 2. Paper market maker (`jevbot run`)
```
feed.py / polymarket.py  ticks (synthetic, or recorded/live Polymarket order book, read only)
state.py                 numeric snapshot, no future data, under 400 tokens
jev.py                   6-question battery, hard deadline, pinned model, drift check
policy.py + pricing.py   thresholds from config.toml, Avellaneda-Stoikov quotes
risk.py                  hard limits against the venue ledger, can veto an order or kill the bot
loop.py                  block loop with fallback ladder: late = hold, Jev down = code-only rules, breach = kill
venue.py                 PAPER ONLY. There is no real-money venue in this repository.
```

## Quality gates (`make check`, also run by CI on Python 3.11 and 3.13)
`ruff check`, `ruff format --check`, `mypy`, `pytest`, plus a scan that fails the build if an API key is committed.
