#!/usr/bin/env python3
"""
Geocode CEOP permit addresses into latitude/longitude using OpenStreetMap's
free Nominatim service, caching every result in the database so it only ever
runs once per address.

Run it after scraping (and again any time new permits are added):

    python geocode_permits.py

It reads addresses from the `permits` table, looks up each distinct address,
and writes the coordinates into a `geocode_cache` table in the same SQLite file.
The Streamlit map app then joins permits to this cache to place them on the map.

Respects Nominatim's usage policy: one request per second and an identifying
User-Agent. For very large address sets this is slow but only happens once.
"""

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DB_PATH = "ceop_permits.db"

# Appended to every address to disambiguate (the registry stores only street +
# house number). Change if you scrape a different city.
CITY = "Novi Sad, Serbia"

# Nominatim requires a real, identifying User-Agent with contact info.
CONTACT = "mladen@sotexsolutions.com"
USER_AGENT = f"ceop-permit-mapper/1.0 ({CONTACT})"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
SLEEP_SEC = 1.1  # >= 1 req/sec per Nominatim policy


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def init_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address      TEXT PRIMARY KEY,  -- raw address from permits.address
            query        TEXT,              -- query string actually sent
            lat          REAL,
            lon          REAL,
            display_name TEXT,              -- what Nominatim matched
            status       TEXT,              -- 'ok' | 'not_found' | 'error'
            geocoded_at  TEXT
        )
        """
    )
    conn.commit()


def normalize_query(address: str, city: str = CITY):
    """Build a Nominatim query from a raw address.

    Addresses often list several house numbers ("Petra I 93, 93A, 93B"); we keep
    the part before the first comma and append the city.
    """
    a = (address or "").strip()
    if not a:
        return None
    a = a.split(",")[0].strip()
    return f"{a}, {city}"


def geocode(query: str):
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data:
        top = data[0]
        return float(top["lat"]), float(top["lon"]), top.get("display_name")
    return None


def store(conn, address, query, lat, lon, display_name, status):
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache "
        "(address, query, lat, lon, display_name, status, geocoded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (address, query, lat, lon, display_name, status,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def geocode_pending(conn, city: str = CITY, sleep_sec: float = SLEEP_SEC, log=log):
    """Geocode every permit address not already cached. Reusable by the scraper.

    Returns (ok, not_found, error) counts. Safe to call repeatedly — only
    addresses missing from geocode_cache are looked up.
    """
    init_cache(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT address FROM permits
        WHERE address IS NOT NULL AND TRIM(address) <> ''
          AND address NOT IN (SELECT address FROM geocode_cache)
        """
    ).fetchall()

    log(f"{len(rows)} new address(es) to geocode (city = {city}).")
    ok = nf = err = 0

    for i, (address,) in enumerate(rows, 1):
        query = normalize_query(address, city)
        try:
            result = geocode(query) if query else None
            if result:
                lat, lon, dn = result
                store(conn, address, query, lat, lon, dn, "ok")
                ok += 1
                log(f"  [{i}/{len(rows)}] ok        {address} -> {lat:.5f}, {lon:.5f}")
            else:
                store(conn, address, query, None, None, None, "not_found")
                nf += 1
                log(f"  [{i}/{len(rows)}] not found  {address}")
        except Exception as exc:  # network / parse errors: record and move on
            store(conn, address, query, None, None, None, "error")
            err += 1
            log(f"  [{i}/{len(rows)}] error      {address} ({exc})")
        time.sleep(sleep_sec)

    log(f"Geocoding done. ok={ok}, not_found={nf}, error={err}.")
    return ok, nf, err


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    geocode_pending(conn, CITY)
    conn.close()
    log(f"Cached in {DB_PATH} (geocode_cache).")


if __name__ == "__main__":
    main()
