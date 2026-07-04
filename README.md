# NEPSE Floorsheet Dataset

A public, continuously-growing dataset of Nepal Stock Exchange (NEPSE)
trade-level floorsheet data, extracted from [merolagani.com](https://merolagani.com/Floorsheet.aspx).

This is an unofficial, community-maintained dataset. It is not affiliated with
NEPSE, SEBON, or Merolagani. Data is provided for research and educational use;
verify anything trading-critical against an official source.

## What's in here

- **`data/floorsheet/YYYY-MM-DD.csv`** — one file per calendar day, every
  individual trade (transaction) reported by NEPSE that day. Non-trading days
  (weekends, holidays) get an empty CSV with just the header, so the file
  list itself is a trading calendar.
- **`data/discrepancies.csv`** — a reconciliation log, one row appended per
  date every time it's fetched: how many records the site reported vs. how
  many rows actually came back, and any errors encountered. Merolagani's own
  page numbering has small, permanent gaps on some dates (confirmed by hand),
  so a small shortfall is an upstream data limitation, not a scraper bug —
  this file is how that's tracked transparently rather than hidden.
- **`PROGRESS.md`** — the continuity ledger. Every backfill run (manual today,
  GitHub Actions daily going forward) appends a summary here: dates covered,
  how many succeeded/were empty/failed. This is the single place to check
  "how far does the dataset go and is anything broken."

### Schema (`data/floorsheet/*.csv`)

| column          | type   | description                              |
|-----------------|--------|-------------------------------------------|
| date            | date   | trading date, `YYYY-MM-DD`                |
| transaction_no  | string | NEPSE transaction id (sortable, increasing within a day) |
| symbol          | string | listed company/security symbol            |
| buyer           | string | buyer broker number                       |
| seller          | string | seller broker number                      |
| quantity        | int    | shares traded                             |
| rate            | float  | price per share (NPR)                     |
| amount          | float  | quantity × rate (NPR)                     |

## Project phases

- **Phase 1 — manual backfill (current):** `scripts/extract_floorsheet.py`
  fetches a date range, is safe to stop/resume (already-downloaded dates are
  skipped), and logs every date's reconciliation to `data/discrepancies.csv`
  and a run summary to `PROGRESS.md`. Output is reviewed and pushed to GitHub
  by hand.
- **Phase 2 — GitHub Actions (planned):** a scheduled daily workflow runs the
  same script for "yesterday" and commits the new CSV, the updated
  discrepancy log, and the updated progress ledger automatically, keeping the
  dataset current without manual work.

## Usage

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# One day
python scripts/extract_floorsheet.py --date 2026-06-30

# A range (resumable — safe to re-run after an interruption)
python scripts/extract_floorsheet.py --start 2020-01-01 --end 2020-12-31

# Re-fetch a date even if already downloaded
python scripts/extract_floorsheet.py --date 2026-06-30 --force
```

`notebooks/extract_data.ipynb` is the original exploratory notebook (includes
an OHLCV roll-up example built from the cached floorsheet CSVs); the script in
`scripts/` is the canonical, maintained entry point.

## License

Code is MIT licensed (see `LICENSE`). The underlying trade data is public
market information published by NEPSE/Merolagani; no additional rights are
claimed over it beyond how it's organized here.
