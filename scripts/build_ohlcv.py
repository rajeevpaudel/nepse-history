#!/usr/bin/env python3
"""Derive a daily OHLCV (open/high/low/close/volume) table per symbol from
the raw floorsheet data in data/floorsheet/<year>/<date>.csv.

The floorsheet has no per-trade timestamp — only `transaction_no`, a
strictly increasing counter within a trading day (verified: constant digit
width per day, see date_csv_path/manifest notes). That ordering stands in
for trade sequence, so per symbol per day:

    open  = rate of the lowest transaction_no  (first trade of the day)
    close = rate of the highest transaction_no (last trade of the day)
    high  = max rate across all of that symbol's contracts that day
    low   = min rate across all of that symbol's contracts that day
    volume   = sum of quantity across contracts
    turnover = sum of amount (quantity * rate) across contracts
    trades   = number of contracts (not related to `volume`, useful as a
               liquidity signal — see README's OHLCV caveats)

This is a derived, best-effort roll-up, not an official exchange OHLC feed —
see the README's "Known data quality issues" section for how floorsheet
shortfalls/gaps propagate into these numbers.

Usage:
    python scripts/build_ohlcv.py --date 2026-07-09       # single day
    python scripts/build_ohlcv.py --start 2020-01-01 --end 2020-12-31
    python scripts/build_ohlcv.py --all                    # every date with a floorsheet file
    python scripts/build_ohlcv.py --all --force             # rebuild even if the OHLCV file exists
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOORSHEET_DIR = os.path.join(REPO_ROOT, "data", "floorsheet")
OHLCV_DIR = os.path.join(REPO_ROOT, "data", "ohlcv")

OHLCV_COLUMNS = ["date", "symbol", "open", "high", "low", "close",
                  "volume", "turnover", "trades"]


def floorsheet_csv_path(iso_date: str, floorsheet_dir: str = FLOORSHEET_DIR) -> str:
    return os.path.join(floorsheet_dir, iso_date[:4], f"{iso_date}.csv")


def ohlcv_csv_path(iso_date: str, ohlcv_dir: str = OHLCV_DIR) -> str:
    return os.path.join(ohlcv_dir, iso_date[:4], f"{iso_date}.csv")


def compute_ohlcv(fs: pd.DataFrame) -> pd.DataFrame:
    """Roll a single day's floorsheet DataFrame up into one row per symbol.

    Sorts by transaction_no as an integer (not string) — digit width is
    constant *within* a day, but differs *across* years (15 digits pre-~2016,
    16 after), so string comparison would be wrong across those boundaries
    were it ever applied cross-day; integer comparison is safe either way.
    """
    fs = fs.copy()
    fs["transaction_no"] = pd.to_numeric(fs["transaction_no"], errors="coerce")
    fs = fs.dropna(subset=["transaction_no"]).sort_values("transaction_no")

    grouped = fs.groupby("symbol", sort=True)
    ohlcv = grouped.agg(
        open=("rate", "first"),
        high=("rate", "max"),
        low=("rate", "min"),
        close=("rate", "last"),
        volume=("quantity", "sum"),
        turnover=("amount", "sum"),
        trades=("transaction_no", "count"),
    ).reset_index()
    return ohlcv[["symbol", "open", "high", "low", "close", "volume", "turnover", "trades"]]


def build_for_date(iso_date: str, floorsheet_dir: str = FLOORSHEET_DIR,
                    ohlcv_dir: str = OHLCV_DIR, force: bool = False) -> dict:
    """Build the OHLCV file for one date from its floorsheet CSV.

    Returns a dict with keys: date, status ("ok" | "no_data" | "skipped" |
    "missing_floorsheet"), symbols (row count written).
    """
    out_path = ohlcv_csv_path(iso_date, ohlcv_dir)
    if os.path.exists(out_path) and not force:
        return {"date": iso_date, "status": "skipped", "symbols": 0}

    fs_path = floorsheet_csv_path(iso_date, floorsheet_dir)
    if not os.path.exists(fs_path):
        return {"date": iso_date, "status": "missing_floorsheet", "symbols": 0}

    fs = pd.read_csv(fs_path, dtype={"transaction_no": str})
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if fs.empty:
        # Non-trading day — write a header-only file so data/ohlcv/ mirrors
        # data/floorsheet/'s "file exists but empty = market closed" convention.
        pd.DataFrame(columns=OHLCV_COLUMNS).to_csv(out_path, index=False)
        return {"date": iso_date, "status": "no_data", "symbols": 0}

    ohlcv = compute_ohlcv(fs)
    ohlcv.insert(0, "date", iso_date)
    ohlcv.to_csv(out_path, index=False)
    return {"date": iso_date, "status": "ok", "symbols": len(ohlcv)}


def existing_floorsheet_dates(floorsheet_dir: str = FLOORSHEET_DIR) -> list:
    dates = []
    for root, _dirs, files in os.walk(floorsheet_dir):
        for fname in files:
            if fname.endswith(".csv"):
                try:
                    dates.append(datetime.strptime(fname[:-4], "%Y-%m-%d").date())
                except ValueError:
                    continue
    return sorted(dates)


def build_all(floorsheet_dir: str = FLOORSHEET_DIR, ohlcv_dir: str = OHLCV_DIR,
              force: bool = False, workers: int = 8) -> list:
    dates = existing_floorsheet_dates(floorsheet_dir)
    iso_dates = [d.isoformat() for d in dates]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(build_for_date, d, floorsheet_dir, ohlcv_dir, force): d
            for d in iso_dates
        }
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 200 == 0:
                print(f"  processed {done}/{len(iso_dates)}...")
    return results


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Single date, YYYY-MM-DD")
    parser.add_argument("--start", help="Range start date, YYYY-MM-DD")
    parser.add_argument("--end", help="Range end date, YYYY-MM-DD (default: today)")
    parser.add_argument("--all", action="store_true",
                         help="Build OHLCV for every date that has a floorsheet file")
    parser.add_argument("--force", action="store_true",
                         help="Rebuild even if the OHLCV file already exists")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers for --all")
    parser.add_argument("--floorsheet-dir", default=FLOORSHEET_DIR)
    parser.add_argument("--ohlcv-dir", default=OHLCV_DIR)
    args = parser.parse_args()

    if not (args.date or args.start or args.all):
        parser.error("provide --date, --start (and optionally --end), or --all")

    if args.all:
        print("Building OHLCV for every date with a floorsheet file...")
        results = build_all(args.floorsheet_dir, args.ohlcv_dir, args.force, args.workers)
    elif args.date:
        results = [build_for_date(args.date, args.floorsheet_dir, args.ohlcv_dir, args.force)]
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today())
        results = [build_for_date(d.isoformat(), args.floorsheet_dir, args.ohlcv_dir, args.force)
                   for d in daterange(start, end)]

    ok = sum(1 for r in results if r["status"] == "ok")
    no_data = sum(1 for r in results if r["status"] == "no_data")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    missing = sum(1 for r in results if r["status"] == "missing_floorsheet")
    print(f"Done. {len(results)} date(s): {ok} ok, {no_data} no-data/holiday, "
          f"{skipped} already existed (skipped), {missing} had no floorsheet file yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
