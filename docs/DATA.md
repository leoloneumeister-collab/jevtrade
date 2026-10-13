# Getting real price data

The page has three price sources (the **Price data** dropdown):

| Source | What it is | Saved to the forward scoreboard? |
|---|---|---|
| Live | Yahoo, then Stooq (free, unofficial, can rate limit) | yes |
| My CSV files | Any daily price CSV you downloaded, kept in `data/` | no (it is history, not a live read) |
| Demo | Fake random prices, to try the page offline | no |

## Easiest path for real data on your Mac
1. Open the stock on Yahoo Finance, click **Historical Data**, set the period to **Max**, frequency **Daily**, click
   **Download**. (Stooq's download button and nasdaq.com's CSV work too.)
2. Import it:
   ```
   jevbot import-csv ~/Downloads/AAPL.csv --symbol AAPL
   ```
3. In the page choose **Price data: My CSV files**, ticker `AAPL`, then **Run past test**.

`import-csv` understands Yahoo, Stooq, plotly and backtesting.py layouts, newest-first files, US dates (m/d/Y) and blank
rows. It uses the **adjusted close** when the file has one, so splits and dividends do not look like crashes.
It refuses files with fewer than 80 days, bad dates, or non-positive prices, and says why.

## Testing different periods
`/api/backtest` takes `skip=N` to move the test window N trading days back in time, and `n` (10 to 250) for its length.
Run several windows on the same stock to see whether a result is consistent or just one lucky stretch.

## Real data used during development (not committed, licenses differ)
- AAPL daily 2015 to 2017: plotly's open `datasets` repo, `finance-charts-apple.csv`.
- GOOG daily 2004 to 2013: ships inside the `backtesting` PyPI package (AGPL, so it is not copied into this repo).

## The honest caveat about old data
Jev may have seen any period before its training cutoff. A good result on 2004 to 2013 prices says very little. Use
recent data when you can, and trust only the **forward scoreboard**: reads made today, judged later.
