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
