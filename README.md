# jevbot

Paper-trading market maker. Jev judges the state, code decides, the risk engine vetoes.
**There is no real-money venue in this repo on purpose.**

```
feed -> state.py (math) -> jev.py (6 atomic questions, 1 call, hard deadline)
     -> policy.py (thresholds from config.toml) -> pricing.py (Avellaneda-Stoikov)
     -> risk.py (veto / kill, reads the venue ledger) -> venue.py (paper) -> journal.jsonl
```

## Your to-do list
1. Get a key at https://console.typesafe.ai/keys, then `export TYPESAFE_API_KEY=...`
2. `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && pytest`
3. `jevbot check` (first real Jev call), then `jevbot models`. Copy the exact model string into `config.toml [jev].model`.
4. Decide the venue and give me its API docs. `feed.py` has the `MarketFeed` seam; the synthetic feed only proves plumbing.
5. Run `jevbot run --mode jev --realtime` on a real feed for 2+ weeks, then `jevbot report`.
6. Only if calibration, the fee check and the rules-only comparison all pass: talk about real money.

## Commands
- `jevbot run --mode rules|double|jev` (`double` is an offline stand-in, not Jev)
- `jevbot compare` same data/costs/limits: rules-only vs decision layer vs no confidence gate
- `jevbot report [journal]` calibration curve for toxic_flow, direction hit-rate, Brier score
- Kill switch writes `./KILL`; the bot will not start until you read it and delete it.

## Notes
- Noul answers carry a probability only; Choice/Score also carry `confidence`.
- The bot refuses `jev-latest` and halts quoting if the server answers with a different model than the pinned one.
- No retries on the Jev call: a retry blows the block deadline. Late means hold.

## Polymarket (read-only feed, paper trading only)
```
jevbot --config config.polymarket.toml markets                      # pick a liquid, mid-range market, copy its YES token id
jevbot --config config.polymarket.toml run --feed polymarket --token <ID> --mode rules --blocks 600 --record runs/raw.jsonl
jevbot --config config.polymarket.toml run --feed replay --replay runs/raw.jsonl --mode rules
```
- **First thing to do with live access:** record a short session and read `runs/raw.jsonl`. The message schemas come from
  the open-source `polymarket-apis` client, not from live traffic. The book tracker compares itself with the venue's own
  best bid/ask on every update and logs a warning if they ever disagree.
- Recorded sessions replay deterministically, so you can re-run the same real data after every config change.
- Unverified assumptions, check them: maker fee is 0 (`maker_fee_bps`), no per-order cost (`order_cost`), `side` in
  `last_trade_price` is the aggressor, size in `price_change` is the new total at that level.
- Paper fills assume half of each print reaches you (`queue_factor`). Real queue position is unknown, so treat paper
  profit as an upper bound.
- Binary markets pay 0 or 1 at resolution. The picker avoids prices near 0/1 and markets ending within 7 days.

## Stock website (runs on YOUR computer)
Mac/Linux, easiest: `./start.sh` (finds Python 3.11+, installs everything, asks for your key, starts the app).
Manual way:
```
pip install -e ".[dev]"
export TYPESAFE_API_KEY=...        # stays on your machine, never sent to the browser
jevbot web                         # then open http://127.0.0.1:8000
```
- **Ask Jev:** a ticker, a look-ahead (1/5/10 days). Jev answers 5 narrow questions about the stock; strict code rules turn
  them into LEAN UP / LEAN DOWN / SIT OUT. Most days should be SIT OUT.
- **Test last 60 days:** replays history day by day (Jev only sees data up to each day) and compares its calls with what
  happened, against the plain baseline of "price rose anyway". Caveat: Jev may have seen that period in training.
- **Forward scoreboard:** every real read is saved to `runs/predictions.jsonl` and checked after the look-ahead passes.
  This is the only test that cannot be fooled by old data. Give it weeks before believing anything.
- Prices come from Yahoo (unofficial) then Stooq, both free and keyless. Not tested live from the build sandbox
  (it blocks them). The "demo data" box uses fake prices so the page works offline.
- Paper only: the app has no broker connection and cannot place orders.

### If the page says "127.0.0.1 refused to connect"
That means no server is listening. Check the terminal where you ran `jevbot web`:
- It must still be open and show "Jev Stock Check is running at http://127.0.0.1:XXXX". Use that exact address, the port can differ.
- If it printed an error or went back to the prompt, the app stopped. Copy the error.
- `jevbot` not found? Use `python -m jevbot web` (Windows: `py -m jevbot web`) from the project folder with the venv active.
- The app opens your browser by itself once the server is ready (`--no-browser` turns that off).
