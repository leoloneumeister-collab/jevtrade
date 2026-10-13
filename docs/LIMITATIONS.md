# What this does not do, and what is not verified

## It does not make money by itself
Jev reads a setup quickly and gives calibrated probabilities. It has no known edge on stocks, and nothing here
creates one. The app is built so that it tells you plainly when there is no edge.

## Known weaknesses of the evidence
- **Backtests can be contaminated.** Jev may have seen the price history it is being tested on during training.
  Treat a good backtest as a reason to keep watching, never as proof. Only the forward scoreboard is clean.
- **Small samples.** A few weeks of daily reads on a handful of tickers is a handful of independent signals.
  The page refuses a verdict under 10; even 30 is thin. Look at many tickers over months.
- **Costs are an assumption.** The 0.10% per trade default is a guess. Spreads on small caps are far larger.
- **Daily data only.** The stock site uses end-of-day bars. It says nothing about intraday behavior.

## Not verified against live services (the build sandbox blocks these hosts)
- Yahoo and Stooq price endpoints (unofficial, free, can change or rate limit without notice).
- The Polymarket websocket schema: built from the open-source `polymarket-apis` client models, not from live traffic.
  Record a session first (`--record`) and check it before trusting any result.
- Paper fills assume half of each trade reaches you. Real queue position is unknown, so paper profit is an upper bound.

## Operational notes
- The Jev API key lives in an environment variable. The launchd example in `docs/SCHEDULE.md` puts it in a file:
  use a key with a low spend limit and `chmod 600` the file.
- The app binds to localhost only. Do not expose it to a network.
- Not financial advice. Paper only. The code contains no way to place a real order.
