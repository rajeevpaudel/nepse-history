#!/usr/bin/env python3
"""Fetch NEPSE floorsheet data directly from the official nepalstock.com.np API.

NEPSE's own site never exposes a date parameter — it only ever shows the
floorsheet for the current/most recently completed session. This is the
"primary source" scraper described in claude.md; the merolagani-based
scraper (scripts/extract_floorsheet.py) is the fallback that supports
arbitrary historical dates.

Two endpoints are involved:
  - market-open: reports whether the market is currently OPEN or CLOSE, plus
    the timestamp ("asOf") of that state. Contracts keep streaming into the
    floorsheet while the market is OPEN, so a day's floorsheet is only
    trustworthy (final, won't change again) once market-open reports CLOSE.
  - floorsheet: paginated list of individual trade contracts, newest first.

Auth: nepalstock.com.np requires an `Authorization: Salter <access_token>`
header on both endpoints. That token is derived from a
`/api/authenticate/prove` challenge plus salts fed into WASM-compiled
functions shipped in the site's own JS bundle (css.wasm). This reverse
engineering is not new — it mirrors the already-working flow in the sibling
repo ../nepse-api (scraper/auth.py, scraper/wasm_loader.py,
scraper/ssl_bundle.py), reimplemented here standalone so this repo doesn't
depend on another one.

The floorsheet endpoint additionally requires a POST body `{"id": <n>}`
where `<n>` is NOT the same "dummyId" trick used by the price endpoint —
it's that value run through a second transformation specific to the
floorsheet page component. Reverse engineered from the site's own
(non-obfuscated, just minified) `nepalstock.js` bundle, function
`getListsOfFloorSheet`:

    base = DUMMY_DATA[dummyId] + dummyId + 2*day        # dummyId = market-open's "id"
    idx  = 1 if base % 10 < 4 else 3
    id   = base + salts[idx]*day - salts[idx - 1]        # salts = [salt1..salt5] from /prove

Without this exact `id`, the endpoint doesn't error — it silently returns
`200 []`, indistinguishable from "no data available right now" unless you
already know to look for it.

Usage:
    python scripts/fetch_nepse.py                # fetch + save if market closed
    python scripts/fetch_nepse.py --dry-run       # just print market status, don't save
    python scripts/fetch_nepse.py --force         # overwrite even if the date's CSV exists
"""
import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import certifi
import httpx
import pandas as pd
import wasmtime
from cryptography import x509
from cryptography.hazmat.primitives import serialization

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "floorsheet")
CACHE_DIR = os.path.join(REPO_ROOT, ".cache", "nepalstock")

BASE_URL = "https://nepalstock.com.np"
PROVE_URL = f"{BASE_URL}/api/authenticate/prove"
MARKET_OPEN_URL = f"{BASE_URL}/api/nots/nepse-data/market-open"
FLOORSHEET_URL = f"{BASE_URL}/api/nots/nepse-data/floorsheet"
WASM_URL = f"{BASE_URL}/assets/prod/css.wasm"
INTERMEDIATE_CERT_URL = "http://cacerts.geotrust.com/GeoTrustTLSRSACAG1.crt"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 "
              "Firefox/147.0")
COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": USER_AGENT,
}
PAGE_SIZE = 500

# From nepalstock.js's UtilsService.dummyData — same table used by the
# price-endpoint's dummyId trick, reused as the base for the floorsheet id.
DUMMY_DATA = [
    147, 117, 239, 143, 157, 312, 161, 612, 512, 804,
    411, 527, 170, 511, 421, 667, 764, 621, 301, 106,
    133, 793, 411, 511, 312, 423, 344, 346, 653, 758,
    342, 222, 236, 811, 711, 611, 122, 447, 128, 199,
    183, 135, 489, 703, 800, 745, 152, 863, 134, 211,
    142, 564, 375, 793, 212, 153, 138, 153, 648, 611,
    151, 649, 318, 143, 117, 756, 119, 141, 717, 113,
    112, 146, 162, 660, 693, 261, 362, 354, 251, 641,
    157, 178, 631, 192, 734, 445, 192, 883, 187, 122,
    591, 731, 852, 384, 565, 596, 451, 772, 624, 691,
]


# --- TLS: NEPSE's chain is missing the GeoTrust intermediate cert that most
# OS/certifi trust stores don't ship, so plain verification fails; fetch it
# once and merge it into a local copy of the certifi bundle. ---
async def get_ssl_bundle(cache_dir: str = CACHE_DIR) -> str:
    base = Path(cache_dir) / "ssl"
    base.mkdir(parents=True, exist_ok=True)
    intermediate_pem = base / "GeoTrustTLSRSACAG1.pem"
    bundle = base / "cacert_with_geotrust.pem"

    if not intermediate_pem.exists():
        async with httpx.AsyncClient() as client:
            resp = await client.get(INTERMEDIATE_CERT_URL, timeout=30)
            resp.raise_for_status()
        cert = x509.load_der_x509_certificate(resp.content)
        intermediate_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    if not bundle.exists() or bundle.stat().st_mtime < intermediate_pem.stat().st_mtime:
        bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + intermediate_pem.read_bytes())

    return str(bundle)


async def download_wasm(cache_dir: str = CACHE_DIR) -> str:
    base = Path(cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    wasm_path = base / "css.wasm"
    if not wasm_path.exists():
        ssl_bundle = await get_ssl_bundle(cache_dir)
        async with httpx.AsyncClient(verify=ssl_bundle) as client:
            resp = await client.get(WASM_URL, headers=COMMON_HEADERS, timeout=30)
            resp.raise_for_status()
        wasm_path.write_bytes(resp.content)
    return str(wasm_path)


def calculate_tokens(wasm_path: str, access_token: str, refresh_token: str,
                      salts: list[int]) -> tuple[str, str]:
    """Splice the raw tokens using indices computed by the site's own WASM
    module — the salts from /prove select which slices of the token string
    to drop. Order and argument permutation here match the site's JS glue
    code exactly (verified against ../nepse-api's working implementation).
    """
    engine = wasmtime.Engine()
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    store = wasmtime.Store(engine)
    module = wasmtime.Module.from_file(engine, wasm_path)
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)

    def call_wasm(fn, *args):
        return exports[fn](store, *args)

    s1, s2, s3, s4, s5 = salts

    a_cdx = call_wasm("cdx", s1, s2, s3, s4, s5)
    a_rdx = call_wasm("rdx", s1, s2, s4, s3, s5)
    a_bdx = call_wasm("bdx", s1, s2, s4, s3, s5)
    a_ndx = call_wasm("ndx", s1, s2, s4, s3, s5)
    a_mdx = call_wasm("mdx", s1, s2, s4, s3, s5)
    new_access = (
        access_token[:a_cdx] + access_token[a_cdx + 1:a_rdx] +
        access_token[a_rdx + 1:a_bdx] + access_token[a_bdx + 1:a_ndx] +
        access_token[a_ndx + 1:a_mdx] + access_token[a_mdx + 1:]
    )

    r_cdx = call_wasm("cdx", s2, s1, s3, s5, s4)
    r_rdx = call_wasm("rdx", s2, s1, s3, s4, s5)
    r_bdx = call_wasm("bdx", s2, s1, s4, s3, s5)
    r_ndx = call_wasm("ndx", s2, s1, s4, s3, s5)
    r_mdx = call_wasm("mdx", s2, s1, s4, s3, s5)
    new_refresh = (
        refresh_token[:r_cdx] + refresh_token[r_cdx + 1:r_rdx] +
        refresh_token[r_rdx + 1:r_bdx] + refresh_token[r_bdx + 1:r_ndx] +
        refresh_token[r_ndx + 1:r_mdx] + refresh_token[r_mdx + 1:]
    )

    return new_access, new_refresh


async def get_tokens() -> tuple[str, str, list[int]]:
    ssl_bundle = await get_ssl_bundle()
    async with httpx.AsyncClient(verify=ssl_bundle) as client:
        resp = await client.get(PROVE_URL, headers=COMMON_HEADERS, timeout=30)
        resp.raise_for_status()
    prove = resp.json()
    salts = [prove["salt1"], prove["salt2"], prove["salt3"], prove["salt4"], prove["salt5"]]
    wasm_path = await download_wasm()
    access, refresh = calculate_tokens(wasm_path, prove["accessToken"], prove["refreshToken"], salts)
    return access, refresh, salts


async def fetch_market_status(access_token: str) -> dict:
    ssl_bundle = await get_ssl_bundle()
    headers = {**COMMON_HEADERS, "Authorization": f"Salter {access_token}"}
    async with httpx.AsyncClient(verify=ssl_bundle) as client:
        resp = await client.get(MARKET_OPEN_URL, headers=headers, timeout=30)
        resp.raise_for_status()
    return resp.json()


def compute_floorsheet_id(dummy_id: int, day: int, salts: list[int]) -> int:
    """See module docstring — reimplements getListsOfFloorSheet() from
    nepalstock.js exactly (including its salt-index selection quirk)."""
    base = DUMMY_DATA[dummy_id] + dummy_id + 2 * day
    idx = 1 if base % 10 < 4 else 3
    return base + salts[idx] * day - salts[idx - 1]


async def fetch_floorsheet_page(client: httpx.AsyncClient, access_token: str,
                                 floorsheet_id: int, page: int, size: int = PAGE_SIZE,
                                 attempts: int = 8) -> dict:
    """The server drops the connection (RemoteProtocolError) if too many
    requests are in flight at once — retry with backoff rather than treat
    that as fatal. A fresh short-lived httpx client per attempt (instead of
    reusing the pooled connection that just got dropped) avoids repeatedly
    tripping over the same half-dead connection.
    """
    headers = {**COMMON_HEADERS, "Authorization": f"Salter {access_token}",
               "Content-Type": "application/json"}
    params = {"page": page, "size": size, "sort": "contractId,desc"}
    for attempt in range(attempts):
        try:
            resp = await client.post(FLOORSHEET_URL, headers=headers, params=params,
                                      json={"id": floorsheet_id}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (httpx.RemoteProtocolError, httpx.TransportError):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(min(1.5 * (attempt + 1), 8.0))


async def fetch_full_floorsheet(access_token: str, floorsheet_id: int,
                                 size: int = PAGE_SIZE, concurrency: int = 3) -> list[dict]:
    """Page through the entire floorsheet, newest contracts first.

    The access token is short-lived (~2 minutes, per the sibling nepse-api
    repo's convention), and a full day can be 150+ pages at the max page
    size the server accepts (500 — larger sizes are silently rejected with
    `[]`). Fetching sequentially risks the token expiring mid-run, so the
    remaining pages are fetched concurrently once we know the total count.
    """
    ssl_bundle = await get_ssl_bundle()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(verify=ssl_bundle, limits=limits) as client:
        first = await fetch_floorsheet_page(client, access_token, floorsheet_id, 0, size)
        if not isinstance(first, dict):
            return []
        content = first["floorsheets"]["content"]
        total_pages = first["floorsheets"]["totalPages"]
        total_elements = first["floorsheets"]["totalElements"]

        rows_by_page: dict[int, list[dict]] = {0: content}
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(page: int):
            async with semaphore:
                data = await fetch_floorsheet_page(client, access_token, floorsheet_id, page, size)
                rows_by_page[page] = data["floorsheets"]["content"]

        await asyncio.gather(*(fetch_one(p) for p in range(1, total_pages)))

    rows = [row for page in sorted(rows_by_page) for row in rows_by_page[page]]
    if len(rows) != total_elements:
        print(f"  warning: fetched {len(rows)} rows but server reported "
              f"totalElements={total_elements}", file=sys.stderr)
    return rows


def rows_to_df(raw_rows: list[dict]) -> pd.DataFrame:
    """Map NEPSE's JSON fields onto the same column schema used by
    scripts/extract_floorsheet.py so both sources merge cleanly.
    """
    records = [{
        "date": r["businessDate"],
        "transaction_no": r["contractId"],
        "symbol": r["stockSymbol"],
        "buyer": r["buyerMemberId"],
        "seller": r["sellerMemberId"],
        "quantity": r["contractQuantity"],
        "rate": r["contractRate"],
        "amount": r["contractAmount"],
    } for r in raw_rows]
    df = pd.DataFrame(records, columns=[
        "date", "transaction_no", "symbol", "buyer", "seller", "quantity", "rate", "amount",
    ])
    return df.drop_duplicates(subset="transaction_no").reset_index(drop=True)


def date_csv_path(iso_date: str, out_dir: str) -> str:
    year = iso_date[:4]
    return os.path.join(out_dir, year, f"{iso_date}.csv")


async def scrape_latest(out_dir: str, force: bool = False, dry_run: bool = False,
                         log=print) -> dict:
    """Fetch whatever business date NEPSE currently has floorsheet data for.

    There's no date parameter to target — this always gets "the current
    day" (or the most recently closed session). Callers that need a
    specific historical date should use scripts/extract_floorsheet.py
    (merolagani) instead; this is only useful for catching the latest day.

    Returns a dict with keys: status ("ok" | "no_data" | "skipped" | "error"),
    date (iso string or None), row_count, note.
    """
    log("Authenticating against nepalstock.com.np...")
    access_token, _, salts = await get_tokens()

    status = await fetch_market_status(access_token)
    log(f"Market status: {status}")
    if status.get("isOpen") != "CLOSE":
        note = "market is OPEN (or status unknown) — floorsheet not final yet"
        log(note)
        return {"status": "no_data", "date": None, "row_count": 0, "note": note}

    dummy_id = status["id"]
    day = datetime.now().day
    floorsheet_id = compute_floorsheet_id(dummy_id, day, salts)

    log("Fetching floorsheet (this may take a while — paginated)...")
    raw_rows = await fetch_full_floorsheet(access_token, floorsheet_id)
    if not raw_rows:
        note = ("floorsheet returned no rows — likely past the data-availability "
                 "window (site clears it ahead of the next session)")
        log(note)
        return {"status": "no_data", "date": None, "row_count": 0, "note": note}

    df = rows_to_df(raw_rows)
    dates_present = df["date"].unique()
    if len(dates_present) != 1:
        log(f"  warning: floorsheet response spans multiple businessDate values: "
            f"{sorted(dates_present)}")
    iso_date = sorted(dates_present)[-1]
    log(f"Business date: {iso_date} — {len(df)} unique contracts")

    if dry_run:
        log("dry-run: not writing CSV.")
        return {"status": "ok", "date": iso_date, "row_count": len(df), "note": "dry-run"}

    out_path = date_csv_path(iso_date, out_dir)
    if os.path.exists(out_path) and not force:
        note = f"{out_path} already exists — skipped (use force to overwrite)"
        log(note)
        return {"status": "skipped", "date": iso_date, "row_count": len(df), "note": note}

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_csv(out_path, index=False)
    log(f"Wrote {len(df)} rows to {out_path}")
    return {"status": "ok", "date": iso_date, "row_count": len(df), "note": ""}


async def run(dry_run: bool, force: bool, out_dir: str) -> int:
    result = await scrape_latest(out_dir, force=force, dry_run=dry_run)
    return 0 if result["status"] in ("ok", "skipped") else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch and print status/summary but don't write a CSV")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite the date's CSV if it already exists")
    parser.add_argument("--out-dir", default=DATA_DIR, help=f"Output directory (default: {DATA_DIR})")
    parser.add_argument("--clear-cache", action="store_true",
                         help="Delete cached SSL bundle / wasm file before running")
    args = parser.parse_args()

    if args.clear_cache and os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)

    return asyncio.run(run(args.dry_run, args.force, args.out_dir))


if __name__ == "__main__":
    sys.exit(main())
