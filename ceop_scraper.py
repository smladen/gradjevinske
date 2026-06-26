#!/usr/bin/env python3
"""
CEOP construction-permit scraper.

Scrapes the public Serbian "Централна евиденција обједињених процедура" (CEOP)
registry of construction permits at https://ceop.apr.gov.rs and stores results
in a local SQLite database. Designed to be run repeatedly (e.g. daily): records
are de-duplicated on the registry's SubmissionId, so re-runs only add genuinely
new cases and refresh the status of existing ones.

How it works
------------
The site is an Angular single-page app. Its search API
(GET /ceopapi/api/search/search) is protected by Google reCAPTCHA v3: every
request must carry a fresh token in a `Recaptcha` header. Those tokens can only
be produced by Google's script running on the real page, so this scraper drives
a headless Chromium via Playwright: it opens the CEOP page, mints a token with
`grecaptcha.execute(...)` for each request, and calls the API from inside the
page context (same-origin, so no CORS issues).

Configure the search in CONFIG below, then run:  python ceop_scraper.py
"""

import math
import sqlite3
import time
from datetime import datetime, date
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CONFIG = {
    # Location: place IDs of the construction site. 802824 == Нови Сад (Novi Sad).
    # To find another place's ID: open the site, use Advanced search ("Напредна
    # претрага"), pick a place in the "Место" field, run a search, and read the
    # `placeIds` value from the request URL in your browser's Network tab.
    "place_ids": ["802824"],

    # Investor name to filter by (plain Latin text). "" = all investors.
    # When set, it is sent to the API in both Latin and Cyrillic (auto-transliterated).
    "investor": "",

    # Only keep permits whose CreatedDate is on/after this date (YYYY-MM-DD).
    # "Issued after 2015" -> 2015-01-01. Filtering is done client-side because the
    # API's date parameters proved unreliable. CEOP itself only launched in 2016,
    # so in practice essentially all records qualify.
    "created_date_from": "2015-01-01",

    # Optional extra free-text filters (leave "" to ignore). These map directly to
    # API parameters and let you narrow the search further.
    "search_string": "",          # part of a case number, e.g. "ROP-NSD"
    "street": "",                 # street name of the construction site
    "submission_number": "",      # exact submission number

    # Output database file.
    "db_path": "ceop_permits.db",

    # Run Chromium with a visible window. reCAPTCHA v3 is score-based; if the
    # registry starts returning empty results in headless mode, set this to True.
    "headful": False,

    # Politeness: seconds to wait between page requests.
    "request_delay_sec": 0.6,

    # Stop after this many permits have been stored this run. 0 = no limit.
    "max_records": 10000,

    # After scraping, geocode any new addresses (OSM Nominatim, ~1 req/sec) into
    # the geocode_cache table so the map app can place them. False = skip.
    "geocode": True,
    "geocode_city": "Novi Sad, Serbia",  # appended to each address when geocoding
}

SITE_URL = "https://ceop.apr.gov.rs/ceopweb/sr-cyrl/home"
API_BASE = "https://ceop.apr.gov.rs/ceopapi/api/search/search"
RECAPTCHA_SITE_KEY = "6LcO8psUAAAAAIc1rYcmQPWJLJ0dcqfn79IvUi-5"
PAGE_SIZE = 10  # API caps page size at 10 regardless of what is requested.

# --------------------------------------------------------------------------- #
# Serbian Latin -> Cyrillic transliteration (for the investor filter)
# --------------------------------------------------------------------------- #
# Order matters: digraphs must be replaced before single letters.
_DIGRAPHS = [
    ("Lj", "Љ"), ("LJ", "Љ"), ("lj", "љ"),
    ("Nj", "Њ"), ("NJ", "Њ"), ("nj", "њ"),
    ("Dž", "Џ"), ("DŽ", "Џ"), ("dž", "џ"),
]
_SINGLES = {
    "A": "А", "B": "Б", "V": "В", "G": "Г", "D": "Д", "Đ": "Ђ", "E": "Е",
    "Ž": "Ж", "Z": "З", "I": "И", "J": "Ј", "K": "К", "L": "Л", "M": "М",
    "N": "Н", "O": "О", "P": "П", "R": "Р", "S": "С", "T": "Т", "Ć": "Ћ",
    "U": "У", "F": "Ф", "H": "Х", "C": "Ц", "Č": "Ч", "Š": "Ш",
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "đ": "ђ", "e": "е",
    "ž": "ж", "z": "з", "i": "и", "j": "ј", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "ć": "ћ",
    "u": "у", "f": "ф", "h": "х", "c": "ц", "č": "ч", "š": "ш",
}


def latin_to_cyrillic(text: str) -> str:
    for lat, cyr in _DIGRAPHS:
        text = text.replace(lat, cyr)
    return "".join(_SINGLES.get(ch, ch) for ch in text)


def build_investor_param(investor: str) -> str:
    """The site sends the investor term wrapped as <latin>..</latin><cyrillic>..</cyrillic>."""
    if not investor:
        return ""
    return f"<latin>{investor}</latin><cyrillic>{latin_to_cyrillic(investor)}</cyrillic>"


def build_search_url(page_number: int) -> str:
    """Build a fully-populated search URL. The API 404s unless every parameter is present."""
    params = {
        "searchString": CONFIG["search_string"],
        "searchStringType": "3",
        "preSubmitDate_From": "",
        "preSubmitDate_To": "",
        "submitedDate_From": "",
        "submitedDate_To": "",
        "submissionTypeGroupIds": "",
        "submissionStatusId": "",
        "organizationId": "",
        "placeIds": ",".join(CONFIG["place_ids"]),
        "street": CONFIG["street"],
        "house": "",
        "municipalityIds": "",
        "parcelNumber": "",
        "investor": build_investor_param(CONFIG["investor"]),
        "investorIdNum": "",
        "submissionNumber": CONFIG["submission_number"],
        "pageNumber": str(page_number),
        "pageSize": str(PAGE_SIZE),
    }
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"{API_BASE}?{query}"


# JavaScript run inside the page: mint a reCAPTCHA token, then call the API.
_FETCH_JS = """
async (args) => {
  const { url, siteKey } = args;
  const token = await new Promise((resolve, reject) => {
    grecaptcha.ready(() => {
      grecaptcha.execute(siteKey, { action: 'search' }).then(resolve).catch(reject);
    });
  });
  const resp = await fetch(url, { headers: { 'Accept': 'application/json', 'Recaptcha': token } });
  const text = await resp.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) {}
  return { status: resp.status, data: data };
}
"""


def keep_record(rec: dict, cutoff: date) -> bool:
    created = rec.get("CreatedDate")
    if not created:
        return True  # keep undated records rather than silently dropping them
    try:
        d = datetime.fromisoformat(created).date()
    except ValueError:
        return True
    return d >= cutoff


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS permits (
            submission_id    TEXT PRIMARY KEY,
            case_number      TEXT,
            status           TEXT,
            submission_type  TEXT,
            organization     TEXT,
            address          TEXT,
            investors        TEXT,
            created_date     TEXT,
            pre_submit_date  TEXT,
            place_ids        TEXT,
            first_seen_at    TEXT,
            last_seen_at     TEXT
        )
        """
    )
    conn.commit()
    return conn


def upsert(conn: sqlite3.Connection, rec: dict, now: str) -> bool:
    """Insert or update one record. Returns True if it was new."""
    sid = rec.get("SubmissionId")
    row = conn.execute(
        "SELECT 1 FROM permits WHERE submission_id = ?", (sid,)
    ).fetchone()
    is_new = row is None
    conn.execute(
        """
        INSERT INTO permits (submission_id, case_number, status, submission_type,
                             organization, address, investors, created_date,
                             pre_submit_date, place_ids, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(submission_id) DO UPDATE SET
            case_number     = excluded.case_number,
            status          = excluded.status,
            submission_type = excluded.submission_type,
            organization    = excluded.organization,
            address         = excluded.address,
            investors       = excluded.investors,
            created_date    = excluded.created_date,
            pre_submit_date = excluded.pre_submit_date,
            place_ids       = excluded.place_ids,
            last_seen_at    = excluded.last_seen_at
        """,
        (
            sid,
            rec.get("ParentLegalUniqueNumber"),
            rec.get("StatusName"),
            rec.get("SubmissionTypeName"),
            rec.get("OrganizationName"),
            rec.get("Address"),
            rec.get("Investors"),
            rec.get("CreatedDate"),
            rec.get("PreSubmitDate"),
            ",".join(CONFIG["place_ids"]),
            now,
            now,
        ),
    )
    return is_new


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Main scrape
# --------------------------------------------------------------------------- #
def scrape() -> None:
    cutoff = datetime.strptime(CONFIG["created_date_from"], "%Y-%m-%d").date()

    log("Starting CEOP scraper")
    log(f"  filters: places={','.join(CONFIG['place_ids'])} "
        f"investor={CONFIG['investor'] or '(none)'} "
        f"created_from={CONFIG['created_date_from']}"
        + (f" search='{CONFIG['search_string']}'" if CONFIG['search_string'] else "")
        + (f" street='{CONFIG['street']}'" if CONFIG['street'] else ""))
    log(f"  database: {CONFIG['db_path']}")

    conn = init_db(CONFIG["db_path"])
    existing = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    log(f"  database opened ({existing} cases already stored)")
    now = datetime.now().isoformat(timespec="seconds")

    total_seen = total_kept = total_new = total_skipped = 0

    with sync_playwright() as p:
        log(f"Launching Chromium ({'headful' if CONFIG['headful'] else 'headless'})...")
        browser = p.chromium.launch(
            headless=not CONFIG["headful"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        log(f"Loading {SITE_URL} ...")
        page.goto(SITE_URL, wait_until="domcontentloaded")
        log("Page loaded, waiting for reCAPTCHA to initialise...")
        try:
            page.wait_for_function(
                "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
                timeout=60000,
            )
        except PlaywrightTimeoutError:
            browser.close()
            raise SystemExit(
                "reCAPTCHA did not initialise within 60s. The site may be blocking "
                "the headless browser. Set  headful = True  in CONFIG and re-run, "
                "and check your internet connection / that www.google.com is reachable."
            )
        log("reCAPTCHA ready.")

        # First request also tells us the total record count.
        log("Requesting page 1 (minting reCAPTCHA token + calling search API)...")
        first = page.evaluate(
            _FETCH_JS, {"url": build_search_url(1), "siteKey": RECAPTCHA_SITE_KEY}
        )
        log(f"  page 1 responded HTTP {first['status']}")
        if first["status"] != 200 or not first["data"]:
            raise SystemExit(
                f"First request failed (HTTP {first['status']}). "
                "The reCAPTCHA score may be too low — try headful=True."
            )

        records_count = first["data"].get("RecordsCount", 0)
        max_records = CONFIG["max_records"]
        total_pages = math.ceil(records_count / PAGE_SIZE) if records_count else 0
        if max_records:
            total_pages = min(total_pages, math.ceil(max_records / PAGE_SIZE))
        cap_note = f", stopping after {max_records} downloaded" if max_records else ""
        log(f"Registry reports {records_count} matching cases "
            f"-> fetching up to {total_pages} page(s) of {PAGE_SIZE}{cap_note}.")

        def handle(result_list, page_no):
            """Process one page. Returns True if the max_records download cap is reached."""
            nonlocal total_seen, total_kept, total_new, total_skipped
            for rec in result_list or []:
                if max_records and total_seen >= max_records:
                    return True
                total_seen += 1
                if not keep_record(rec, cutoff):
                    total_skipped += 1
                    log(f"    - skip (before {CONFIG['created_date_from']}): "
                        f"{rec.get('ParentLegalUniqueNumber')} ({rec.get('CreatedDate')})")
                    continue
                total_kept += 1
                is_new = upsert(conn, rec, now)
                if is_new:
                    total_new += 1
                log(f"    {'+ NEW ' if is_new else '~ upd '}"
                    f"{rec.get('ParentLegalUniqueNumber')} | "
                    f"{rec.get('SubmissionTypeName')} | {rec.get('StatusName')}")
            return bool(max_records and total_seen >= max_records)

        cap_reached = handle(first["data"].get("ResultList"), 1)
        conn.commit()
        log(f"  page 1/{total_pages} done (kept {total_kept}, new {total_new}, "
            f"skipped {total_skipped})")

        for page_no in range(2, total_pages + 1):
            if cap_reached:
                log(f"Reached the {max_records}-record limit, stopping.")
                break
            time.sleep(CONFIG["request_delay_sec"])
            log(f"Requesting page {page_no}/{total_pages} ...")
            res = page.evaluate(
                _FETCH_JS,
                {"url": build_search_url(page_no), "siteKey": RECAPTCHA_SITE_KEY},
            )
            if res["status"] != 200 or not res["data"]:
                log(f"  ! page {page_no} returned HTTP {res['status']}, skipping.")
                continue
            cap_reached = handle(res["data"].get("ResultList"), page_no)
            conn.commit()
            log(f"  page {page_no}/{total_pages} done (kept {total_kept}, "
                f"new {total_new}, skipped {total_skipped})")

        log("Closing browser...")
        browser.close()

    log(
        f"Scraping done. Fetched {total_seen} records, kept {total_kept} after the "
        f"{CONFIG['created_date_from']} date filter ({total_skipped} skipped), "
        f"{total_new} new this run."
    )

    if CONFIG["geocode"]:
        try:
            from geocode_permits import geocode_pending
            log("Geocoding new addresses (OSM Nominatim, ~1 req/sec)...")
            geocode_pending(conn, CONFIG["geocode_city"], log=log)
        except ImportError:
            log("Skipping geocoding: geocode_permits.py not found next to this script.")
        except Exception as exc:
            log(f"Geocoding step failed ({exc}); permits were still saved.")

    conn.close()
    log(f"Saved to {CONFIG['db_path']} (table: permits, geocode_cache).")


if __name__ == "__main__":
    scrape()
