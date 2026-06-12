"""
Listing Monitor - core engine.
Logic ported 1:1 from AMZ_FINAL.gs (v7.1):
  - same RapidAPI endpoint (Real-Time Amazon Data, product-details, th=1)
  - same parse_input / domain detection
  - same parse_price (US 1,234.56 and EU 1.234,56 / 29,99)
  - same hasRealStrikethrough truth check for List Price / Strikethrough / Discount %
  - same Buybox source: main_buy_box.seller, fallback product_byline
  - same normalise/values_match comparison
  - mismatch flags ONLY on Title / Sale Price / # of Reviews
Adds what Sheets could not: SQLite history of every run.
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests

DB_PATH = os.environ.get("LM_DB_PATH", os.path.join(os.path.dirname(__file__), "listing_monitor.db"))

RAPIDAPI_HOST = "real-time-amazon-data.p.rapidapi.com"

DOMAIN_TO_COUNTRY = {
    "amazon.com": "US",
    "amazon.co.uk": "GB",
    "amazon.de": "DE",
    "amazon.fr": "FR",
    "amazon.it": "IT",
    "amazon.es": "ES",
    "amazon.ca": "CA",
    "amazon.co.jp": "JP",
    "amazon.com.au": "AU",
    "amazon.nl": "NL",
}

MASTER_COLUMNS = [
    "Input (ASIN/URL)", "Title", "List Price", "Sale Price", "# of Reviews",
    "Buybox Winner", "Strikethrough?", "Discount %", "Main Image",
    "Image 1", "Image 2", "Image 3", "Image 4", "Image 5", "Image 6", "Image 7",
]

# Only these are red-flagged on mismatch (same rule as the Sheets script v7.1)
MISMATCH_FIELDS = ["Title", "Sale Price", "# of Reviews"]


# ---------------------------------------------------------------- parsing ---

def parse_input(raw: str):
    """Extract (asin, domain) from a bare ASIN or any Amazon URL."""
    s = (raw or "").strip()
    domain = "amazon.com"
    for key in DOMAIN_TO_COUNTRY:
        if key in s:
            domain = key
            break
    m = (re.search(r"/dp/([A-Z0-9]{10})", s, re.I)
         or re.search(r"/gp/product/([A-Z0-9]{10})", s, re.I)
         or re.search(r"([A-Z0-9]{10})(?:[/?&]|$)", s, re.I))
    if m:
        return m.group(1).upper(), domain
    if re.fullmatch(r"[A-Z0-9]{10}", s, re.I):
        return s.upper(), domain
    return None, domain


def parse_price(raw):
    """US (1,234.56) and EU (1.234,56 / 29,99) -> float, else None."""
    if raw is None or raw == "":
        return None
    s = re.sub(r"[^0-9.,]", "", str(raw))
    if not s:
        return None
    last_comma, last_dot = s.rfind(","), s.rfind(".")
    if last_comma > last_dot:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def normalise(v) -> str:
    """Same as the Sheets normalise(): lower, strip $/euro/pound, commas, spaces, trailing .0+."""
    s = str(v if v is not None else "").strip().lower()
    s = re.sub(r"[$\u20ac\u00a3,\s]", "", s)
    s = re.sub(r"\.0+$", "", s)
    return s


def values_match(master_val, live_val) -> bool:
    return normalise(master_val) == normalise(live_val)


# ------------------------------------------------------------------ fetch ---

def fetch_amazon(asin: str, domain: str, api_key: str, timeout: int = 30) -> dict:
    country = DOMAIN_TO_COUNTRY.get(domain or "amazon.com", "US")
    url = f"https://{RAPIDAPI_HOST}/product-details"
    resp = requests.get(
        url,
        params={"asin": asin, "country": country, "th": "1"},
        headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": api_key},
        timeout=timeout,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"HTTP {resp.status_code}: non-JSON response")
    if resp.status_code != 200 or not payload.get("data"):
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(payload)[:300]}")
    return payload["data"]


def extract_fields(p: dict) -> dict:
    d = {}
    d["Title"] = p.get("product_title") or ""
    d["Sale Price"] = p.get("product_price") or ""
    d["# of Reviews"] = p.get("product_num_ratings") or ""

    sale_num = parse_price(p.get("product_price"))
    list_num = parse_price(p.get("product_original_price"))
    has_real_strike = (list_num is not None and sale_num is not None and list_num > sale_num)

    d["List Price"] = (p.get("product_original_price") or "") if has_real_strike else ""
    d["Strikethrough?"] = "Yes" if has_real_strike else "No"
    d["Discount %"] = f"{round((1 - sale_num / list_num) * 100)}%" if has_real_strike else ""

    buybox = (p.get("main_buy_box") or {}).get("seller") or p.get("product_byline") or ""
    d["Buybox Winner"] = buybox

    d["Main Image"] = p.get("product_photo") or ""
    photos = p.get("product_photos") or []
    for i in range(1, 8):
        d[f"Image {i}"] = photos[i] if len(photos) > i else ""
    return d


# --------------------------------------------------------------- database ---

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS masterfile (
            input TEXT PRIMARY KEY,
            data  TEXT NOT NULL,           -- JSON of MASTER_COLUMNS values
            updated_at TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            total INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 0,
            mismatched_rows INTEGER DEFAULT 0
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id),
            input TEXT NOT NULL,
            asin TEXT,
            marketplace TEXT,
            live TEXT,                     -- JSON of extracted live fields
            title_match INTEGER,           -- 1/0, NULL = master empty (not judged)
            price_match INTEGER,
            reviews_match INTEGER,
            sale_price_num REAL,
            reviews_num INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        )""")
    conn.commit()
    return conn


def save_masterfile_rows(rows: list[dict]):
    """Replace the entire masterfile with the given rows (list of dicts keyed by MASTER_COLUMNS)."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM masterfile")
    for r in rows:
        inp = str(r.get("Input (ASIN/URL)", "")).strip()
        if not inp:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO masterfile (input, data, updated_at) VALUES (?,?,?)",
            (inp, json.dumps({c: ("" if r.get(c) is None else str(r.get(c))) for c in MASTER_COLUMNS}), now),
        )
    conn.commit()
    conn.close()


def load_masterfile() -> list[dict]:
    conn = get_db()
    rows = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM masterfile ORDER BY rowid")]
    conn.close()
    return rows


# -------------------------------------------------------------- audit run ---

def run_audit(api_key: str, pacing_seconds: float = 0.5, progress_cb=None) -> int:
    """Audit every masterfile row. Returns run_id. progress_cb(done, total, label) optional."""
    if not api_key:
        raise RuntimeError("RAPIDAPI_KEY is not set. Put it in .env or the environment.")

    master_rows = load_masterfile()
    if not master_rows:
        raise RuntimeError("Masterfile is empty. Import it first.")

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("INSERT INTO runs (started_at, total) VALUES (?, ?)", (now, len(master_rows)))
    run_id = cur.lastrowid
    conn.commit()

    ok = 0
    mismatched_rows = 0
    total = len(master_rows)

    for i, mrow in enumerate(master_rows):
        raw_input = str(mrow.get("Input (ASIN/URL)", "")).strip()
        asin, domain = parse_input(raw_input)
        created = datetime.now(timezone.utc).isoformat()

        if progress_cb:
            progress_cb(i, total, raw_input)

        if not asin:
            conn.execute(
                "INSERT INTO results (run_id, input, asin, marketplace, live, error, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, raw_input, None, domain, None, f"Cannot extract ASIN from: {raw_input}", created),
            )
            conn.commit()
            continue

        try:
            live = extract_fields(fetch_amazon(asin, domain, api_key))
        except Exception as e:  # noqa: BLE001 - record any fetch failure per-row
            conn.execute(
                "INSERT INTO results (run_id, input, asin, marketplace, live, error, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, raw_input, asin, domain, None, str(e)[:500], created),
            )
            conn.commit()
            time.sleep(pacing_seconds)
            continue

        # Mismatch verdicts - ONLY Title / Sale Price / # of Reviews.
        # Empty master value => NULL (not judged), same as the Sheets script.
        def verdict(field: str):
            mv = str(mrow.get(field, "") or "").strip()
            if mv == "":
                return None
            return 1 if values_match(mv, str(live.get(field, "")).strip()) else 0

        t_m, p_m, r_m = verdict("Title"), verdict("Sale Price"), verdict("# of Reviews")
        if 0 in (t_m, p_m, r_m):
            mismatched_rows += 1
        ok += 1

        reviews_num = None
        rv = re.sub(r"[^\d]", "", str(live.get("# of Reviews", "")))
        if rv:
            reviews_num = int(rv)

        conn.execute(
            "INSERT INTO results (run_id, input, asin, marketplace, live,"
            " title_match, price_match, reviews_match, sale_price_num, reviews_num, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, raw_input, asin, domain, json.dumps(live),
             t_m, p_m, r_m, parse_price(live.get("Sale Price")), reviews_num, None, created),
        )
        conn.commit()
        time.sleep(pacing_seconds)

    conn.execute(
        "UPDATE runs SET finished_at=?, ok=?, mismatched_rows=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), ok, mismatched_rows, run_id),
    )
    conn.commit()
    conn.close()
    if progress_cb:
        progress_cb(total, total, "done")
    return run_id


# ---------------------------------------------------------------- queries ---

def list_runs(limit: int = 50) -> list[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


def get_results(run_id: int) -> list[dict]:
    conn = get_db()
    out = []
    for r in conn.execute("SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,)):
        d = dict(r)
        d["live"] = json.loads(d["live"]) if d["live"] else {}
        out.append(d)
    conn.close()
    return out


def asin_history(asin: str) -> list[dict]:
    conn = get_db()
    out = []
    q = ("SELECT res.created_at, res.sale_price_num, res.reviews_num, res.title_match,"
         " res.price_match, res.reviews_match, res.live, res.error"
         " FROM results res WHERE res.asin=? ORDER BY res.id")
    for r in conn.execute(q, (asin,)):
        d = dict(r)
        d["live"] = json.loads(d["live"]) if d["live"] else {}
        out.append(d)
    conn.close()
    return out
