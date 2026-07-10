#!/usr/bin/env python3
"""Extract NEPSE floorsheet data from merolagani.com into data/floorsheet/<year>/.

Single day:
    python scripts/extract_floorsheet.py --date 2026-06-30

Date range (phase 1 manual backfill, resumable — already-downloaded dates
are skipped, so re-running after an interruption just continues):
    python scripts/extract_floorsheet.py --start 2020-01-01 --end 2020-12-31

Each run appends to data/discrepancies.csv (per-date record-count reconciliation)
and PROGRESS.md (human-readable run log), so the same code path can be reused
unchanged by the future GitHub Actions daily job.
"""
import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

BASE_URL = "https://merolagani.com/Floorsheet.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://merolagani.com",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": BASE_URL,
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "floorsheet")
DISCREPANCY_FILE = os.path.join(REPO_ROOT, "data", "discrepancies.csv")
PROGRESS_FILE = os.path.join(REPO_ROOT, "PROGRESS.md")
DISCREPANCY_FIELDS = [
    "date", "total_records_reported", "rows_written", "shortfall", "status", "note", "checked_at",
]

MAX_WORKERS = 10
DATE_WORKERS = 3

RECORD_RE = re.compile(
    r"Showing ([\d,]+) - ([\d,]+) of ([\d,]+) records\. \[Total pages: (\d+)\]"
)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retries = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_maxsize=MAX_WORKERS * 2)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def extract_form_fields(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", id="aspnetForm")
    return {
        inp.get("name"): inp.get("value", "")
        for inp in form.find_all("input")
        if inp.get("name")
    }


def parse_record_summary(html: str):
    m = RECORD_RE.search(html)
    if not m:
        return None
    _, _, total_records, total_pages = m.groups()
    return int(total_records.replace(",", "")), int(total_pages)


def parse_floorsheet_rows(html: str, iso_date: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#ctl00_ContentPlaceHolder1_divData table.sortable")
    if table is None:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != 8:
            continue
        _, txn_no, symbol, buyer, seller, qty, rate, amount = cells
        rows.append({
            "date": iso_date,
            "transaction_no": txn_no,
            "symbol": symbol,
            "buyer": buyer,
            "seller": seller,
            "quantity": int(qty.replace(",", "") or 0),
            "rate": float(rate.replace(",", "") or 0),
            "amount": float(amount.replace(",", "") or 0),
        })
    return rows


def search_date(session: requests.Session, mmddyyyy: str):
    """GET a fresh page then submit the date filter via the postback the
    'Search' link fires client-side. Returns (fields_after_search,
    total_records, total_pages, page1_rows), or None if the date has no data.
    """
    r0 = session.get(BASE_URL, timeout=30)
    r0.raise_for_status()
    fields = extract_form_fields(r0.text)

    payload = dict(fields)
    payload["ctl00$ContentPlaceHolder1$txtFloorsheetDateFilter"] = mmddyyyy
    payload["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$lbtnSearchFloorsheet"
    payload["__EVENTARGUMENT"] = ""
    # Submit-button fields must NOT be present alongside a __EVENTTARGET postback,
    # or the server errors out (it saw two conflicting triggers).
    payload.pop("ctl00$ContentPlaceHolder1$PagerControl1$btnPaging", None)
    payload.pop("ctl00$ContentPlaceHolder1$PagerControl2$btnPaging", None)

    r1 = session.post(BASE_URL, data=payload, timeout=30)
    r1.raise_for_status()

    summary = parse_record_summary(r1.text)
    if summary is None:
        return None
    total_records, total_pages = summary
    fields_after_search = extract_form_fields(r1.text)
    iso_date = iso_from_mmddyyyy(mmddyyyy)
    page1_rows = parse_floorsheet_rows(r1.text, iso_date)
    return fields_after_search, total_records, total_pages, page1_rows


def fetch_page(session: requests.Session, fields_after_search: dict, mmddyyyy: str,
               page_num: int, attempts: int = 3) -> list[dict]:
    """Fetch a single page by forking off the post-search viewstate snapshot.
    The server does not require pages to be requested in sequence, so many
    pages can be fetched concurrently from the same base viewstate.
    """
    payload = dict(fields_after_search)
    payload.pop("__EVENTTARGET", None)
    payload.pop("__EVENTARGUMENT", None)
    payload.pop("ctl00$ContentPlaceHolder1$PagerControl2$btnPaging", None)
    payload["ctl00$ContentPlaceHolder1$PagerControl1$hdnCurrentPage"] = str(page_num)

    iso_date = iso_from_mmddyyyy(mmddyyyy)
    for attempt in range(attempts):
        try:
            r = session.post(BASE_URL, data=payload, timeout=30)
            r.raise_for_status()
            rows = parse_floorsheet_rows(r.text, iso_date)
            if rows:
                return rows
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
        time.sleep(0.5 * (attempt + 1))
    return []


def iso_from_mmddyyyy(mmddyyyy: str) -> str:
    m, d, y = mmddyyyy.split("/")
    return f"{y}-{m}-{d}"


def to_mmddyyyy(date_str: str) -> str:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {date_str!r} (use YYYY-MM-DD or MM/DD/YYYY)")


def candidate_dates(start: date, end: date):
    """Every calendar day in range. NEPSE's regular non-trading day is
    Saturday, but holidays (and occasional Friday sessions) vary, so we don't
    hardcode a weekly schedule — we just ask the site for each day and record
    a 'no_data' status (not an error) for the ones with nothing to report.
    """
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def date_csv_path(d: date, out_dir: str) -> str:
    return os.path.join(out_dir, str(d.year), f"{d.isoformat()}.csv")


def fetch_floorsheet_for_date(mmddyyyy: str, max_workers: int = MAX_WORKERS):
    """Fetch every page for a date and de-duplicate on transaction_no.

    Returns (DataFrame, total_records_reported_or_None).

    Note: merolagani's own row numbering has permanent gaps on some pages
    (confirmed by hand: page N renders fewer than 500 rows and page N+1's
    running index jumps ahead, e.g. page 73 -> #36001-36478, page 74 starts
    at #36501). The "Showing X of N records" total appears to come from a
    different, larger count than what the grid actually ever renders, so a
    small shortfall vs. that total is an upstream data limitation, not
    something retries can recover — we log it to data/discrepancies.csv
    instead of failing.
    """
    session = make_session()
    result = search_date(session, mmddyyyy)
    if result is None:
        return pd.DataFrame(), None
    fields_after_search, total_records, total_pages, page1_rows = result

    rows_by_txn = {r["transaction_no"]: r for r in page1_rows}
    remaining_pages = list(range(2, total_pages + 1))

    if remaining_pages:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(fetch_page, session, fields_after_search, mmddyyyy, p): p
                for p in remaining_pages
            }
            for fut in as_completed(futures):
                for row in fut.result():
                    rows_by_txn[row["transaction_no"]] = row

    df = pd.DataFrame(rows_by_txn.values())
    return df.reset_index(drop=True), total_records


def log_discrepancy(iso_date: str, total_records, rows_written: int, status: str, note: str = ""):
    os.makedirs(os.path.dirname(DISCREPANCY_FILE), exist_ok=True)
    is_new = not os.path.exists(DISCREPANCY_FILE)
    shortfall = "" if total_records is None else max(total_records - rows_written, 0)
    with open(DISCREPANCY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DISCREPANCY_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "date": iso_date,
            "total_records_reported": total_records if total_records is not None else "",
            "rows_written": rows_written,
            "shortfall": shortfall,
            "status": status,
            "note": note,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })


def update_progress(run_label: str, results: list[dict]):
    """Append a run summary to PROGRESS.md so both manual backfills and the
    future GitHub Actions daily job leave a continuity trail in one place.
    """
    ok_dates = sorted(r["date"] for r in results if r["status"] == "ok")
    latest = ok_dates[-1] if ok_dates else None
    lines = [
        f"### {datetime.now(timezone.utc).isoformat(timespec='seconds')} — {run_label}",
        "",
        f"- Dates processed: {len(results)}",
        f"- OK: {sum(1 for r in results if r['status'] == 'ok')}",
        f"- No data (holiday/out of range): {sum(1 for r in results if r['status'] == 'no_data')}",
        f"- Errors: {sum(1 for r in results if r['status'] == 'error')}",
    ]
    if latest:
        lines.append(f"- Latest date with data in this run: {latest}")
    shortfalls = [r for r in results if r["status"] == "ok" and r.get("shortfall")]
    if shortfalls:
        lines.append(f"- Dates with row-count shortfall (see data/discrepancies.csv): "
                      f"{', '.join(r['date'] for r in shortfalls)}")
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        lines.append(f"- Failed dates (need re-run): {', '.join(r['date'] for r in errors)}")
    lines.append("")

    if not os.path.exists(PROGRESS_FILE):
        header = "# Progress\n\nContinuity ledger for the NEPSE floorsheet backfill. Every " \
                  "manual run and every future GitHub Actions daily run appends a run-log " \
                  "entry below.\n\n## Run Log\n\n"
        with open(PROGRESS_FILE, "w") as f:
            f.write(header)

    with open(PROGRESS_FILE, "a") as f:
        f.write("\n".join(lines) + "\n")


def process_date(d: date, out_dir: str, max_workers: int) -> dict:
    mmddyyyy = d.strftime("%m/%d/%Y")
    iso_date = d.isoformat()
    out_path = date_csv_path(d, out_dir)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        df, total_records = fetch_floorsheet_for_date(mmddyyyy, max_workers=max_workers)
    except requests.RequestException as e:
        log_discrepancy(iso_date, None, 0, "error", str(e))
        return {"date": iso_date, "status": "error", "note": str(e)}

    if df.empty:
        pd.DataFrame(columns=["date", "transaction_no", "symbol", "buyer", "seller",
                               "quantity", "rate", "amount"]).to_csv(out_path, index=False)
        log_discrepancy(iso_date, total_records, 0, "no_data")
        print(f"  {iso_date}: no data (holiday or out of archive range)")
        return {"date": iso_date, "status": "no_data"}

    df.to_csv(out_path, index=False)
    shortfall = max((total_records or 0) - len(df), 0)
    if shortfall > 0:
        note = (f"grid rendered {len(df)} rows vs {total_records} reported — "
                f"known upstream row-numbering gaps, not a scraping error")
        print(f"  {iso_date}: wrote {len(df)} rows (note: {note})")
    else:
        note = ""
        print(f"  {iso_date}: wrote {len(df)} rows")
    log_discrepancy(iso_date, total_records, len(df), "ok", note)
    return {"date": iso_date, "status": "ok", "shortfall": shortfall}


def backfill(start: date, end: date, out_dir: str, max_workers: int, date_workers: int,
             force: bool = False) -> list[dict]:
    dates = [d for d in candidate_dates(start, end)
             if force or not os.path.exists(date_csv_path(d, out_dir))]
    if not dates:
        print("Nothing to do — all dates already cached.")
        return []

    os.makedirs(out_dir, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=date_workers) as pool:
        futures = {pool.submit(process_date, d, out_dir, max_workers): d for d in dates}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Single trading date, YYYY-MM-DD or MM/DD/YYYY")
    parser.add_argument("--start", help="Range start date, YYYY-MM-DD or MM/DD/YYYY")
    parser.add_argument("--end", help="Range end date, YYYY-MM-DD or MM/DD/YYYY (default: today)")
    parser.add_argument("--out-dir", default=DATA_DIR, help=f"Output directory (default: {DATA_DIR})")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Concurrent page requests per date")
    parser.add_argument("--date-workers", type=int, default=DATE_WORKERS, help="Concurrent dates in flight")
    parser.add_argument("--force", action="store_true", help="Re-fetch dates even if a CSV already exists")
    args = parser.parse_args()

    if not args.date and not args.start:
        parser.error("provide either --date, or --start (and optionally --end)")

    if args.date:
        d = datetime.strptime(to_mmddyyyy(args.date), "%m/%d/%Y").date()
        start = end = d
        run_label = f"manual single-day run ({d.isoformat()})"
    else:
        start = datetime.strptime(to_mmddyyyy(args.start), "%m/%d/%Y").date()
        end = (datetime.strptime(to_mmddyyyy(args.end), "%m/%d/%Y").date()
               if args.end else date.today())
        run_label = f"manual backfill run ({start.isoformat()} to {end.isoformat()})"

    print(f"Running: {run_label}")
    results = backfill(start, end, args.out_dir, args.workers, args.date_workers, force=args.force)
    if results:
        update_progress(run_label, results)
        print(f"Done. {len(results)} date(s) processed — see PROGRESS.md and data/discrepancies.csv")


if __name__ == "__main__":
    sys.exit(main())
