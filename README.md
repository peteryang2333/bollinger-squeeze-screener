# Bollinger / TTM Squeeze Screener

A daily research tool that scans a universe of stocks and ranks the ones whose
**Bollinger Bands are squeezing** — i.e. volatility has contracted and a big
move (up *or* down) is likely imminent. Built to feed a "next-day watchlist"
for manual review, not to auto-trade.

## Why this exists
When a sector is trending hard (money flooding in), individual names go into
*band-walk* — wide bands, price riding the upper band — not squeezes. Squeezes
happen during **consolidation / low volatility**. So the value of scanning the
*whole market* (not just the hot sector) is to catch the quiet names coiling
up before their move. This script automates that hunt.

## Two definitions of "squeeze" (both implemented)
1. **BB-width contraction** (`bb_squeeze`) — current Bollinger Band width is in
   the tightest bucket of the stock's own trailing 100-day history. Detects
   volatility contraction regardless of direction.
2. **TTM Squeeze** (John Carter, [reference](https://github.com/vegatek/ttm-squeeze))
   — Bollinger Bands are *completely inside* the Keltner Channels. Stricter and
   directional-aware: when BB re-emerges and momentum > 0, the release bias is up.

A name is a **candidate** if either flag is true. Candidates are ranked by a
composite score (tighter BB-width = higher, TTM-on = bonus).

> ⚠️ **A squeeze only tells you volatility will expand — NOT the direction.**
> Always confirm with momentum sign + volume + your own MA/structure rules
> (e.g. buy the *release* on volume, ideally with a pullback to MA20), then
> place a stop at the coil low.

## Project layout
```
squeeze_core.py      indicators: Bollinger, Keltner, TTM squeeze, momentum, percentile rank
data_provider.py     OHLCV fetch: yfinance (batched, primary) + stockanalysis.com fallback (no key)
universe.py          build ticker list (watchlist.txt + S&P 500 / Nasdaq-100 / Russell 2000 from Wikipedia)
screener.py          orchestrate: fetch -> compute -> rank -> write reports
config.yaml          all tunables
watchlist.txt        your hand-picked names (highest priority)
results/             squeeze_<DATE>.{json,md,html}  (generated daily)
.github/workflows/   GitHub Actions scheduled run
```

## Run locally
```bash
pip install -r requirements.txt
python screener.py          # scans watchlist + S&P 500 + Nasdaq-100 + Russell 2000, writes results/
```
Outputs go to `results/squeeze_YYYY-MM-DD.{json,md,html}`. The `.html` is a
self-contained page you can open directly — that's the "morning view".

## Tuning
Edit `config.yaml`:
- `universe.use_sp500` — scan the full S&P 500 (broad) or just your watchlist.
- `indicators.bw_squeeze_pctile` — tightness threshold (lower = stricter).
- `filters.min_avg_volume` — drop illiquid names.
- `schedule.cron_utc` — when the daily job runs.

## Deploy (daily automation)
Two options:
- **GitHub Actions** (recommended, free, no server): the included workflow runs
  Mon–Fri at 21:30 UTC (= 05:30 Beijing, after the US close) and commits the
  day's results back to the repo. Needs only `contents: write`.
- **Your VM** (cron + git push): same `screener.py`, scheduled via `crontab`.

## Data note
Primary source is `yfinance` (correct instrument mapping, batched download —
fast for the full ~3,500-name universe). If yfinance is rate-limited, the script
falls back to `stockanalysis.com` per-ticker (no API key needed). Each ticker's
history is cached in `data/` (gitignored) and only refreshed when stale (>20h),
so re-runs are cheap. Some non-US listings (e.g. European ADRs) may not resolve
on the US endpoint — keep those in `watchlist.txt` and sanity-check prices.
