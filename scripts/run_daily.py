#!/usr/bin/env python3
"""Daily orchestrator for the NEPSE floorsheet public dataset — the entry
point the GitHub Actions workflow calls once a day after market close.

Source priority per date:
  - The most recent date (today, or whatever business date NEPSE's official
    site currently has) is tried via scripts/fetch_nepse.py first — it's the
    primary source, but its API has no date parameter, so it can only ever
    give us "whatever NEPSE currently shows".
  - Every other missing date (older catch-up days, or the latest date if
    fetch_nepse failed/was unavailable) falls back to
    scripts/extract_floorsheet.py (merolagani), which supports arbitrary
    historical dates.

"Missing" here means: no data/floorsheet/<date>.csv file for that date yet.
That file-existence check is the source of truth for what's already been
captured (both scripts already skip/no-op past dates that have a file), not
manifest.json — the manifest is provenance/audit trail, recorded alongside.

A single date's failure never aborts the run — every date gets its own
try/except and its own manifest entry, so one broken day doesn't block the
others. The run exits non-zero only if at least one date ended in a genuine
"failed" state (as opposed to "missing", which is the expected outcome for
holidays/weekends).

Usage:
    python scripts/run_daily.py                       # catch up through today
    python scripts/run_daily.py --date 2026-07-09       # (re)process a single date
    python scripts/run_daily.py --max-catchup-days 5    # cap how far back to catch up
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import fetch_nepse as nepse
import extract_floorsheet as merolagani

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "floorsheet")
MANIFEST_PATH = os.path.join(REPO_ROOT, "manifest.json")

DEFAULT_MAX_CATCHUP_DAYS = 10


def load_manifest() -> dict:
    if not os.path.exists(MANIFEST_PATH):
        return {}
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump(dict(sorted(manifest.items())), f, indent=2)
        f.write("\n")


def record(manifest: dict, iso_date: str, source: str, status: str,
           row_count: int, note: str = "") -> None:
    """Add/update a manifest entry. Bumps retries (and preserves the note
    trail) instead of silently clobbering a prior successful entry.
    """
    existing = manifest.get(iso_date)
    retries = existing.get("retries", 0) if existing else 0
    if existing and existing.get("status") == "ok":
        retries += 1
        note = note or f"re-fetched over existing ok entry (was {existing.get('source')})"
    manifest[iso_date] = {
        "source": source,
        "status": status,
        "row_count": row_count,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "retries": retries,
        "notes": note,
    }


def count_csv_rows(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return max(sum(1 for _ in f) - 1, 0)  # minus header


def existing_csv_dates(out_dir: str) -> set:
    if not os.path.isdir(out_dir):
        return set()
    found = set()
    for fname in os.listdir(out_dir):
        if fname.endswith(".csv"):
            try:
                found.add(datetime.strptime(fname[:-4], "%Y-%m-%d").date())
            except ValueError:
                continue
    return found


def catchup_dates(out_dir: str, today: date, max_catchup_days: int) -> list:
    have = existing_csv_dates(out_dir)
    earliest = today - timedelta(days=max_catchup_days - 1)
    dates = [earliest + timedelta(days=i) for i in range((today - earliest).days + 1)]
    return [d for d in dates if d not in have]


def run_merolagani_fallback(d: date, out_dir: str, manifest: dict, today: date,
                             extra_note: str = "") -> dict:
    result = merolagani.process_date(d, out_dir, merolagani.MAX_WORKERS)
    iso_date = result["date"]
    if result["status"] == "ok":
        row_count = count_csv_rows(merolagani.date_csv_path(d, out_dir))
        note = extra_note
        if result.get("shortfall"):
            note = (note + "; " if note else "") + f"row-count shortfall={result['shortfall']}"
        record(manifest, iso_date, "merolagani", "ok", row_count, note)
    elif result["status"] == "no_data":
        if d == today:
            # For any earlier date "no data" reliably means holiday/weekend —
            # both sources have long since settled. For *today* it might just
            # mean the session hasn't happened/closed yet, so don't lock in a
            # permanent empty CSV (process_date wrote one) — remove it so
            # tomorrow's run (or a later run today) retries this date instead
            # of treating it as forever missing.
            out_path = merolagani.date_csv_path(d, out_dir)
            if os.path.exists(out_path):
                os.remove(out_path)
            note = (extra_note + "; " if extra_note else "") + "no data yet today — will retry next run"
            record(manifest, iso_date, "missing", "pending", 0, note)
        else:
            note = (extra_note + "; " if extra_note else "") + "no session (holiday/weekend or out of archive range)"
            record(manifest, iso_date, "missing", "missing", 0, note)
    else:
        note = (extra_note + "; " if extra_note else "") + f"merolagani error: {result.get('note', '')}"
        record(manifest, iso_date, "merolagani", "failed", 0, note)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Process a single date only (YYYY-MM-DD), skipping catch-up scan")
    parser.add_argument("--max-catchup-days", type=int, default=DEFAULT_MAX_CATCHUP_DAYS,
                         help=f"How many days back to look for gaps (default: {DEFAULT_MAX_CATCHUP_DAYS}). "
                              f"Deep historical backfill is scripts/extract_floorsheet.py --start/--end, "
                              f"run manually — not this script's job.")
    parser.add_argument("--out-dir", default=DATA_DIR, help=f"Output directory (default: {DATA_DIR})")
    args = parser.parse_args()

    today = date.today()
    manifest = load_manifest()

    if args.date:
        targets = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        targets = catchup_dates(args.out_dir, today, args.max_catchup_days)

    if not targets:
        print("Nothing to do — no missing dates in the catch-up window.")
        return 0

    print(f"Target dates: {[d.isoformat() for d in targets]}")
    results = []
    most_recent = max(targets)

    # Step 1: primary source, only for the most recent target — fetch_nepse
    # has no way to ask for an older date, it always returns whatever NEPSE
    # currently has.
    remaining = list(targets)
    if most_recent >= today - timedelta(days=1):
        print(f"\n=== Trying primary source (NEPSE) for the latest date ===")
        try:
            nepse_result = asyncio.run(nepse.scrape_latest(args.out_dir, force=False))
        except Exception as e:
            nepse_result = {"status": "error", "date": None, "row_count": 0, "note": str(e)}

        if nepse_result["status"] in ("ok", "skipped") and nepse_result["date"]:
            got_date = datetime.strptime(nepse_result["date"], "%Y-%m-%d").date()
            if got_date in remaining:
                record(manifest, nepse_result["date"], "nepse", "ok",
                       nepse_result["row_count"], nepse_result.get("note", ""))
                results.append({"date": nepse_result["date"], "status": "ok"})
                remaining.remove(got_date)
            else:
                print(f"  NEPSE returned {nepse_result['date']}, which is outside "
                      f"the current target set — ignoring (already covered).")
        else:
            print(f"  NEPSE primary source unavailable: {nepse_result.get('note', nepse_result['status'])}")
            # most_recent still needs a fallback attempt below, tagged with why nepse failed.

    # Step 2: merolagani fallback for everything still missing (older
    # catch-up days, plus the latest day if NEPSE didn't cover it).
    if remaining:
        print(f"\n=== Falling back to merolagani for: {[d.isoformat() for d in remaining]} ===")
    for d in sorted(remaining):
        extra_note = ""
        if d == most_recent and d >= today - timedelta(days=1):
            extra_note = "nepse did not return this date"
        r = run_merolagani_fallback(d, args.out_dir, manifest, today, extra_note)
        results.append(r)

    save_manifest(manifest)
    merolagani.update_progress(f"run_daily ({today.isoformat()})", results)

    failed = [r for r in results if r["status"] == "error"]
    ok = [r for r in results if r["status"] == "ok"]
    missing = [r for r in results if r["status"] == "no_data"]
    print(f"\nDone. {len(ok)} ok, {len(missing)} missing (holiday/no session), "
          f"{len(failed)} failed. manifest.json updated.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
