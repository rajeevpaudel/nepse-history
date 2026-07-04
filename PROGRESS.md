# Progress

Continuity ledger for the NEPSE floorsheet backfill. Every manual run and every
future GitHub Actions daily run appends a run-log entry below via
`scripts/extract_floorsheet.py`'s `update_progress()`. Don't hand-edit the run
log section — let the script own it.

## Status

- **Phase 1 (current):** manual backfill using `scripts/extract_floorsheet.py --start ... --end ...`,
  results reviewed and pushed to GitHub by hand.
- **Phase 2 (planned):** a scheduled GitHub Actions workflow runs the same
  script daily for "yesterday", commits the new CSV + updated
  `data/discrepancies.csv` + this file, so the dataset stays current without
  manual intervention.

## Run Log

### 2026-07-04T15:43:41+00:00 — manual backfill run (2026-06-30 to 2026-06-30)

- Dates processed: 1
- OK: 1
- No data (holiday/out of range): 0
- Errors: 0
- Latest date with data in this run: 2026-06-30
- Dates with row-count shortfall (see data/discrepancies.csv): 2026-06-30

### 2026-07-04T15:45:51+00:00 — manual backfill run (2026-07-03 to 2026-07-03)

- Dates processed: 1
- OK: 1
- No data (holiday/out of range): 0
- Errors: 0
- Latest date with data in this run: 2026-07-03
- Dates with row-count shortfall (see data/discrepancies.csv): 2026-07-03

### 2026-07-04T15:46:04+00:00 — manual single-day run (2026-06-27)

- Dates processed: 1
- OK: 0
- No data (holiday/out of range): 1
- Errors: 0

