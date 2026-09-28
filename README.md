# NEPSE Floorsheet & OHLCV Dataset

[![Daily floorsheet fetch](https://github.com/rajeevpaudel/nepse-history/actions/workflows/daily_floorsheet.yml/badge.svg)](https://github.com/rajeevpaudel/nepse-history/actions/workflows/daily_floorsheet.yml)
[![License: MIT](https://img.shields.io/badge/code%20license-MIT-blue.svg)](LICENSE)
[![Data size](https://img.shields.io/badge/data-~5GB-lightgrey.svg)](#dataset-structure)

A free, open, continuously-updated dataset of **trade-level floorsheet data**
from the Nepal Stock Exchange (NEPSE) — every individual contract (buyer,
seller, quantity, rate) for every trading day back to **2015-01-01** — plus a
**derived daily OHLCV (open/high/low/close/volume) table per symbol**, rolled
up from that same floorsheet data.

NEPSE's own website only ever shows the *current* trading day's floorsheet;
there is no official API or archive for historical days, and no OHLC feed at
all below intraday-provider paywalls. This repo exists to close that gap: it
scrapes, reconciles, and permanently publishes the daily floorsheet — and
derives a ready-to-use OHLCV table from it — so researchers, students, and
developers don't have to re-solve this problem or pay for it.

> **Unofficial, community-maintained.** Not affiliated with NEPSE, SEBON, or
> Merolagani. Provided for research and educational use — verify anything
> trading-critical against an official source.

---

## Quick facts

| | |
|---|---|
| Coverage | 2015-01-01 → present (daily, growing) |
| Floorsheet rows | ~193M+ individual trade contracts across ~4,100 trading days |
| OHLCV rows | ~521K symbol-days (one row per symbol per trading day) |
| Update cadence | Daily, automated (GitHub Actions, after market close) |
| Floorsheet format | `data/floorsheet/YYYY/YYYY-MM-DD.csv` — one file per trading day |
| OHLCV format | `data/ohlcv/YYYY/YYYY-MM-DD.csv` — one file per trading day, one row per symbol |
| Size | ~4.8 GB floorsheet, ~42 MB OHLCV (uncompressed) |
| License | Code: MIT. Data: public exchange information, see [License](#license) |

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Dataset structure](#dataset-structure)
- [Schema: floorsheet](#schema-datafloorsheetyearyyyy-mm-ddcsv)
- [OHLCV dataset (derived)](#ohlcv-dataset-derived)
- [How the data is collected](#how-the-data-is-collected)
- [Known data quality issues](#known-data-quality-issues)
- [Provenance & auditability](#provenance--auditability)
- [Usage](#usage)
- [Reproducing / running it yourself](#reproducing--running-it-yourself)
- [Contributing](#contributing)
- [Roadmap](#roadmap)
- [License](#license)
- [Citation](#citation)

---

## Why this exists

Floorsheet data — the full list of individual buy/sell contracts executed
each session — is the most granular public record NEPSE produces, but it is
practically unarchived:

- NEPSE's official site (`nepalstock.com.np`) exposes only the current or
  most recently closed session, with no date parameter.
- Third-party portals like Merolagani do support historical dates, but each
  day has to be scraped one at a time, and pages are paginated with no bulk
  export.
- No official historical bulk download exists for either source.

This repo automates that collection once, publicly, so the same scraping
work doesn't need to be repeated by every student, researcher, or developer
who wants NEPSE trade history. It follows the same "scrape once daily, commit
flat files, let git history be the audit trail" pattern used by projects like
[`fivethirtyeight/data`](https://github.com/fivethirtyeight/data) and
[`nytimes/covid-19-data`](https://github.com/nytimes/covid-19-data): no
database, no infrastructure to run — just versioned files anyone can `git
clone` or `curl` directly.

## Dataset structure

```
data/
  floorsheet/
    2015/
      2015-01-01.csv         # one file per calendar day, grouped by year
      2015-01-02.csv
      ...
    2026/
      ...
      2026-07-09.csv
  ohlcv/
    2015/
      2015-01-01.csv          # derived: one row per symbol, rolled up from the same-day floorsheet
      ...
    2026/
      ...
      2026-07-09.csv
  discrepancies.csv         # reconciliation log — see "Known data quality issues"
manifest.json                # per-date provenance: source, status, row count, timestamp
PROGRESS.md                  # human-readable run log / continuity ledger
scripts/
  fetch_nepse.py              # primary source: official NEPSE site (current day only)
  extract_floorsheet.py       # fallback / historical source: Merolagani (any date)
  run_daily.py                 # daily orchestrator: try NEPSE, fall back to Merolagani, derive OHLCV
  build_ohlcv.py                # derives data/ohlcv/ from data/floorsheet/
.github/workflows/
  daily_floorsheet.yml          # scheduled job that runs the pipeline and commits results
  test_nepse_scrape.yml          # manual, isolated health check for the NEPSE-only path
```

Data is partitioned by year rather than kept as one flat directory of ~4,100+
files (and growing by ~250/year): GitHub's own file browser struggles past a
few thousand entries in a single directory, and year partitions let you
`git sparse-checkout set data/floorsheet/2024` to pull down just the years
you need instead of the full multi-GB history.

Non-trading days (Saturdays and public holidays — NEPSE's weekend is
Friday–Saturday) still get a CSV, containing only the header row. This makes
`data/floorsheet/` itself a de facto trading calendar: a file with zero data
rows means "market was closed," not "collection failed."

## Schema (`data/floorsheet/<year>/*.csv`)

| Column | Type | Description |
|---|---|---|
| `date` | date | Trading date, `YYYY-MM-DD` |
| `transaction_no` | string | Exchange contract/transaction ID (monotonically increasing within a day) |
| `symbol` | string | Listed security symbol |
| `buyer` | string | Buyer broker (member) ID |
| `seller` | string | Seller broker (member) ID |
| `quantity` | integer | Shares traded in this contract |
| `rate` | float | Price per share, NPR |
| `amount` | float | `quantity × rate`, NPR |

There is **no trade timestamp** — floorsheet data is ordered by contract
sequence only, not wall-clock time. This is a limitation of the source data
itself (both NEPSE and Merolagani expose it this way), not of this pipeline.
Any intraday/OHLC analysis built on top of this dataset can only be
sequence-resolution, not time-resolution.

## OHLCV dataset (derived)

Raw floorsheet rows are useful, but most day-to-day analysis (charting,
backtesting, screening) wants a daily open/high/low/close/volume series per
symbol, not tens of thousands of raw contracts. `data/ohlcv/<year>/<date>.csv`
is that table, rebuilt directly from the floorsheet by
[`scripts/build_ohlcv.py`](scripts/build_ohlcv.py) — never hand-edited, and
regenerated automatically as part of the daily pipeline.

### Schema (`data/ohlcv/<year>/*.csv`)

| Column | Type | Description |
|---|---|---|
| `date` | date | Trading date, `YYYY-MM-DD` |
| `symbol` | string | Listed security symbol |
| `open` | float | Rate of that symbol's first contract of the day |
| `high` | float | Highest rate across all of that symbol's contracts that day |
| `low` | float | Lowest rate across all of that symbol's contracts that day |
| `close` | float | Rate of that symbol's last contract of the day |
| `volume` | integer | Total shares traded (sum of `quantity`) |
| `turnover` | float | Total value traded, NPR (sum of `amount`) |
| `trades` | integer | Number of contracts that day (not the same as `volume`) |

### How it's derived — and why that matters

Since floorsheet rows carry no timestamp, `open`/`close` are **not** based on
market-open/market-close wall-clock prices — they're the rate of the
first and last contract **by `transaction_no` order**, which is a strictly
increasing per-day sequence counter, used here as a proxy for chronological
order. `high`/`low` are true across-the-day extremes regardless of ordering,
so those two are as reliable as the underlying floorsheet itself.

Practical consequences worth knowing before using this as an OHLC feed:

- **It is a derived, best-effort roll-up, not an official exchange OHLC
  feed.** Cross-check against NEPSE's own published daily summary before
  using it for anything trading-critical.
- **Every floorsheet limitation propagates here.** The row-count shortfalls
  and the 2023-02-16 outlier described in
  [Known data quality issues](#known-data-quality-issues) affect `volume`
  and `turnover` on the same dates — a missing contract can't be aggregated
  if it was never in the underlying floorsheet.
- **Thinly-traded symbols** with a single contract on a given day will have
  `open == high == low == close` — this is a correct reflection of a
  single print, not a data bug.
- **Non-trading days** get a header-only CSV, exactly like
  `data/floorsheet/`, so the same "empty file = market closed" convention
  applies to both datasets.
- **Rebuilding is fully idempotent** — `python scripts/build_ohlcv.py --all`
  regenerates every date from scratch from the floorsheet CSVs already in
  the repo; nothing is carried over or assumed between days.

## How the data is collected

Each day, after market close (~16:00 NPT), a scheduled GitHub Actions
workflow ([`daily_floorsheet.yml`](.github/workflows/daily_floorsheet.yml))
runs a two-tier pipeline:

1. **Primary source — official NEPSE API** (`scripts/fetch_nepse.py`). Talks
   directly to `nepalstock.com.np`'s internal API, which requires
   reverse-engineered token derivation (see the script's docstring for
   details). Since this API never exposes a date parameter, it can only ever
   be used for the latest session.
2. **Fallback source — Merolagani** (`scripts/extract_floorsheet.py`). Used
   whenever the NEPSE fetch fails for any reason (site/API change, no data
   yet, transient network error) and for all historical backfill, since it's
   the only one of the two that supports arbitrary past dates.
3. **Provenance recorded** in `manifest.json` — which source served each
   date, row count, timestamp, and retry count. This is the single source of
   truth for "where did this day's data come from" and "is a date missing
   because of a holiday or because collection failed."
4. **Derive OHLCV.** `scripts/build_ohlcv.py` rolls that day's floorsheet up
   into `data/ohlcv/<year>/<date>.csv` (see [OHLCV dataset](#ohlcv-dataset-derived)).
5. **Commit and push.** The new day's floorsheet and OHLCV CSVs, the
   manifest, and the progress log are committed back to this repo. Git
   history is the append-only audit trail; nothing is silently rewritten.

A single date's failure never blocks the rest of the run (each date gets its
own try/except), and the workflow files a GitHub issue automatically if
either source fails — see
[`.github/workflows/daily_floorsheet.yml`](.github/workflows/daily_floorsheet.yml).

Historical backfill (2015 through the day this pipeline started running) was
a one-time batch job against Merolagani, since NEPSE's live API cannot serve
past dates at all.

## Known data quality issues

Transparency here is a first-class feature of this dataset, not an
afterthought — every discrepancy below is logged per-date in
[`data/discrepancies.csv`](data/discrepancies.csv), not hidden.

### 1. Small per-day row shortfalls (upstream, not a scraper bug)

Merolagani's own floorsheet grid reports a "total records" count that,
on many dates, doesn't quite match the number of distinct rows actually
rendered across its paginated grid — confirmed by hand to be a gap in
Merolagani's own row numbering, not a pagination or dedup bug on our side.

- Affects **2,427 of ~4,100 collected trading days** (~59%).
- Typical shortfall is small: **median 37 rows**, 90th percentile 158 rows,
  out of tens of thousands of rows on a typical day.
- One extreme outlier exists (**2023-02-16**, shortfall of 35,078 rows) — a
  known one-off upstream anomaly on Merolagani's side for that date, flagged
  in `data/discrepancies.csv` rather than silently smoothed over.
- Every occurrence is recorded with the exact `total_records_reported` vs.
  `rows_written` in `data/discrepancies.csv`, so anyone doing precise volume
  reconciliation can identify and account for affected dates directly.

### 2. Transient fetch failures (retried, not silently dropped)

**112 date-fetch attempts** during the historical backfill failed outright
(mostly `Read timed out` / connection errors against `merolagani.com`) rather
than returning partial data. These are logged with `status=error` and the
exact exception message in `data/discrepancies.csv`, and were retried in
later runs — check `manifest.json` for the date in question to see whether a
later successful attempt superseded the failure (via the `retries` field).

### 3. No trade timestamps

As noted in the schema section: floorsheet rows are ordered by contract
sequence, not wall-clock time. Neither source (NEPSE nor Merolagani) exposes
intraday timestamps, so this dataset cannot support minute-level or
timestamp-based analysis — only daily aggregates and sequence-based ordering
within a day.

### 4. NEPSE primary source has no history

The official NEPSE API (`scripts/fetch_nepse.py`) only ever returns the
current/most-recently-closed session — it cannot be used to fetch or
re-verify any date other than "today." All historical data (everything
except the most recent trading day at any given time) comes from Merolagani.
This is recorded per-date in `manifest.json`'s `source` field so consumers
know which pipeline produced which day.

### 5. Non-trading days

Weekends (Friday–Saturday) and public holidays produce an empty (header-only)
CSV and a `missing` status in the manifest — this is expected, not a failure.
Do not interpret a missing/empty date as a collection gap without first
checking `manifest.json`.

## Provenance & auditability

- **`manifest.json`** — the canonical per-date record: which source served
  the data (`nepse` / `merolagani` / `merolagani-backfill` / `missing`),
  `status`, `row_count`, `fetched_at` timestamp, and `retries`. A prior
  successful entry is never silently overwritten — re-fetches bump `retries`
  and append a note explaining why.
- **`data/discrepancies.csv`** — append-only reconciliation log, one row per
  fetch attempt per date, recording reported-vs-collected row counts and any
  errors.
- **`PROGRESS.md`** — a human-readable run log: every backfill batch and
  every daily automated run appends a summary (dates processed, ok/missing/
  error counts).
- **Git history itself** is the deepest audit trail — every commit is one
  day's data landing, so `git log -- data/floorsheet/2023/2023-02-16.csv`
  shows exactly when and how that file was produced or corrected.

## Usage

Clone (or sparse-checkout, given the repo's size) and read directly with any
CSV-aware tool. The full repo is several GB (`.git` history + `data/`), so if
you just want the current files without the whole daily-commit history, use
a shallow clone:

```bash
# Full clone (includes full git history / audit trail)
git clone https://github.com/rajeevpaudel/nepse-history.git
cd nepse-history
```

```bash
# Shallow clone — latest snapshot only, much faster/smaller
git clone --depth 1 https://github.com/rajeevpaudel/nepse-history.git
cd nepse-history
```

```python
import pandas as pd

df = pd.read_csv("data/floorsheet/2026/2026-07-09.csv")
df.groupby("symbol")["amount"].sum().sort_values(ascending=False).head(10)
```

For most analysis, the much smaller derived OHLCV table is the better
starting point:

```python
import pandas as pd

ohlcv = pd.read_csv("data/ohlcv/2026/2026-07-09.csv")
ohlcv.sort_values("turnover", ascending=False).head(10)
```

To load a date range efficiently, use `manifest.json` to skip non-trading
days rather than trying to read every calendar date (same pattern works for
`data/ohlcv/` — just swap the directory):

```python
import json, pandas as pd, pathlib

manifest = json.load(open("manifest.json"))
traded_dates = [d for d, v in manifest.items() if v["status"] == "ok"]

frames = [pd.read_csv(f"data/floorsheet/{d[:4]}/{d}.csv") for d in sorted(traded_dates)]
floorsheet = pd.concat(frames, ignore_index=True)
```

## Reproducing / running it yourself

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Latest session from the official NEPSE site (only works once market is closed)
python scripts/fetch_nepse.py --dry-run

# A specific historical date via the Merolagani fallback
python scripts/extract_floorsheet.py --date 2026-06-30

# A date range (resumable — already-downloaded dates are skipped)
python scripts/extract_floorsheet.py --start 2020-01-01 --end 2020-12-31

# Full daily pipeline (what the GitHub Actions workflow runs) — also
# derives OHLCV for every date it touches
python scripts/run_daily.py

# Rebuild the OHLCV dataset directly, e.g. after correcting a floorsheet file
python scripts/build_ohlcv.py --date 2026-07-09
python scripts/build_ohlcv.py --all --force   # rebuild everything from scratch
```

`notebooks/` contains exploratory analysis, including the original prototype
that `scripts/build_ohlcv.py` was derived from.

## Contributing

Issues and PRs are welcome, particularly:

- Reports of new discrepancies or anomalies found in specific dates.
- Fixes for scraper breakage (both NEPSE and Merolagani occasionally change
  their site structure/auth without notice — see
  [`test_nepse_scrape.yml`](.github/workflows/test_nepse_scrape.yml) for an
  isolated way to check whether the primary source is still working).
- Additional historical or corroborating sources for cross-validation.

Please don't open PRs that hand-edit `data/`, `manifest.json`, or
`PROGRESS.md` — those are pipeline-generated and any manual edit will be
overwritten or conflict with the next automated run.

## Roadmap

- [x] Daily OHLCV roll-up as a derived, lower-frequency companion dataset
      (sequence-resolution only — see [OHLCV dataset](#ohlcv-dataset-derived)).
- [ ] Parquet mirror alongside the per-day CSVs, once the CSV pipeline has a
      few more weeks of stable daily runs behind it.
- [ ] Periodic export to Kaggle / Hugging Face Datasets for easier bulk
      access without cloning the full git history.
- [ ] Per-symbol OHLCV pivot (e.g. `NABIL.csv` with its full daily history)
      as a convenience mirror of the day-partitioned OHLCV files, for users
      who want a single time series per symbol rather than per-day files.

## License

- **Code** (everything under `scripts/`, `.github/`) is MIT licensed — see
  [`LICENSE`](LICENSE).
- **Data** is public market information originally published by NEPSE and
  redistributed here as collected via Merolagani. No additional rights are
  claimed over the underlying trade data beyond how it is organized and
  presented in this repository. If you plan to redistribute or build a
  commercial product on this data, verify the terms of use of the original
  sources (NEPSE, Merolagani) independently.

## Citation

If you use this dataset in research, please cite it as:

```
Paudel, R. (2026). NEPSE Floorsheet Dataset [Data set].
GitHub. https://github.com/rajeevpaudel/nepse-history
```
