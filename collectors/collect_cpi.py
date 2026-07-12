"""
Monthly CPI Collector — MOSPI eSankhyiki (base year 2024, All-India, Combined).
Fetches item-level CPI Index for the dashboard commodities and writes them
to data/cpi/mospi_monthly.json.

No authentication required — public GET endpoints.

Usage:
  python collect_cpi.py               → normal daily collection
  python collect_cpi.py --discover    → scan CPI catalog and print oil-related items
                                        (one-time helper; codes then get hardcoded above)
"""

import ssl
import sys
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DATA_FILE = "data/cpi/mospi_monthly.json"
BASE = "https://api.mospi.gov.in"
IST = timezone(timedelta(hours=5, minutes=30))

# commodity key -> CPI item_code (base year 2024)
#   sugar was previously mis-labeled "sugarcane" (item 111 is Sugar, not sugarcane).
#   Oil codes (soybean/mustard/groundnut/sunflower oil) still TODO — run
#     `python collect_cpi.py --discover`  to identify them, then paste here.
ITEM_CODES = {
    "paddy": 1,       # Rice
    "wheat": 2,       # Wheat
    "maize": 6,       # Maize and its products
    "onion": 92,      # Onion
    "tur": 99,        # Arhar, tur
    "gram": 104,      # Gram: whole
    "sugar": 111,     # Sugar
    # "groundnut_oil": ?,
    # "mustard_oil":   ?,
    # "soybean_oil":   ?,
    # "sunflower_oil": ?,
}
# CPI item name (as returned by API, lowercased) -> commodity key
NAME_TO_KEY = {
    "rice": "paddy",
    "wheat": "wheat",
    "maize and its products": "maize",
    "onion": "onion",
    "arhar, tur": "tur",
    "gram: whole": "gram",
    "sugar": "sugar",
    # Oils — item names will be filled in once the discover step returns them
    # "groundnut oil": "groundnut_oil",
    # "mustard oil": "mustard_oil",
    # "soyabean oil": "soybean_oil",
    # "sunflower oil": "sunflower_oil",
}
MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

# MoSPI server needs legacy SSL renegotiation
_ctx = ssl.create_default_context()
_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
_ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45, context=_ctx) as r:
        return json.loads(r.read().decode())


def fetch_codes(item_codes, year, month_code):
    """Fetch a batch of item codes for one (year, month). Returns list of rows or []."""
    codes = ",".join(str(c) for c in item_codes)
    try:
        j = get("/api/cpi/getCPIData", {
            "base_year": "2024", "series": "Current", "level": "Item",
            "state_code": "1", "sector_code": "3",
            "item_code": codes, "year": str(year), "month_code": str(month_code),
            "Format": "JSON", "limit": str(len(item_codes) + 5)
        })
        return j.get("data", []) or []
    except Exception as e:
        print(f"    fetch error for codes {item_codes[:3]}...: {e}")
        return []


def fetch_month(year, month_code):
    """Fetch all configured items for one (year, month)."""
    codes = list(ITEM_CODES.values())
    return fetch_codes(codes, year, month_code)


def candidate_months():
    """Most recent months first, going back ~6 months from today."""
    now = datetime.now(IST)
    y, m = now.year, now.month
    out = []
    for _ in range(6):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


# ============================================================
# Discovery mode — scan the CPI catalog for oil-related items
# ============================================================

def discover_oils():
    """Scan CPI item codes 1..MAX and print anything oil-related.

    Uses the last month with data (found by scanning back from today).
    Codes and names are printed so they can be hardcoded into ITEM_CODES
    and NAME_TO_KEY above.
    """
    print("Discovering oil-related CPI items...")
    print("=" * 60)

    # First, find the latest month that has ANY data — use item code 1 (Rice)
    # as a probe since we already know it's populated.
    latest_year, latest_month = None, None
    for (y, m) in candidate_months():
        r = fetch_codes([1], y, m)
        if r:
            latest_year, latest_month = y, m
            print(f"Scanning against latest available month: {MONTHS[m]} {y}\n")
            break
    if not latest_year:
        print("Could not find any recent month with CPI data. Aborting discovery.")
        return

    # Sweep item codes in batches. CPI catalog runs roughly 1..~300 for
    # base 2024; scan 1..500 to be safe.
    BATCH = 40
    MAX_CODE = 500
    hits = []
    all_names = []

    for start in range(1, MAX_CODE + 1, BATCH):
        batch = list(range(start, min(start + BATCH, MAX_CODE + 1)))
        rows = fetch_codes(batch, latest_year, latest_month)
        for row in rows:
            code = row.get("item_code") or row.get("itemCode") or row.get("code")
            name = (row.get("item") or "").strip()
            all_names.append((code, name))
            low = name.lower()
            if "oil" in low or "vanaspati" in low:
                hits.append((code, name))
        # be gentle on the API
        time.sleep(1.5)

    print("\n" + "=" * 60)
    print("ALL OIL / FAT / VANASPATI ITEMS FOUND:")
    print("=" * 60)
    if not hits:
        print("(no matches)")
    else:
        for code, name in sorted(hits, key=lambda x: (str(x[0]), x[1])):
            print(f"  code={code!s:>5}   {name}")

    # Also print anything mentioning our target seed names, in case oils
    # are labeled differently.
    print("\n" + "=" * 60)
    print("ALSO — items matching seed keywords:")
    print("=" * 60)
    keywords = ["soya", "soyabean", "soybean", "mustard", "groundnut",
                "sunflower", "palm", "rapeseed", "sesame"]
    for code, name in all_names:
        low = name.lower()
        if any(k in low for k in keywords):
            print(f"  code={code!s:>5}   {name}")

    print("\nDone. Paste the 4 oil codes into ITEM_CODES and NAME_TO_KEY at the")
    print("top of collect_cpi.py, then re-run without --discover.")


# ============================================================
# Normal collection
# ============================================================

def load_existing():
    """Load mospi_monthly.json or return a fresh skeleton."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "source": "MOSPI eSankhyiki (All-India, Combined)",
        "base_year": "2024",
        "updated": None,
        "notes": [
            "Monthly CPI per commodity, base year 2024 (index=100).",
            "Grows monthly via collector.",
        ],
        "commodities": {},
    }


def migrate_sugarcane_to_sugar(data):
    """One-time migration: old key 'sugarcane' (item 111 is Sugar) → 'sugar'."""
    coms = data.get("commodities") or {}
    if "sugarcane" in coms and "sugar" not in coms:
        coms["sugar"] = coms.pop("sugarcane")
        print("  (migrated existing 'sugarcane' entries to 'sugar')")


def main_collect():
    print(f"Collecting CPI at {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}...")

    # Find the latest month that has data
    rows, year, mcode = [], None, None
    for (y, m) in candidate_months():
        r = fetch_month(y, m)
        if r:
            rows, year, mcode = r, y, m
            print(f"Latest CPI available: {MONTHS[m]} {y} ({len(r)} items)")
            break
        else:
            print(f"  no data for {MONTHS[m]} {y}")
    if not rows:
        print("FAILED: no CPI data found in the last 6 months")
        return

    # Map rows to commodities
    parsed = {}
    for row in rows:
        name = (row.get("item") or "").strip().lower()
        key = NAME_TO_KEY.get(name)
        if not key:
            continue
        try:
            idx = float(row.get("index"))
        except (TypeError, ValueError):
            continue
        parsed[key] = {
            "year": str(year),
            "month": MONTHS[mcode],
            "month_code": mcode,
            "index": idx,
            "base_year": "2024",
            "source": "MOSPI eSankhyiki (All-India, Combined)",
        }

    if not parsed:
        print("FAILED: could not match any items to commodities")
        return

    # Ensure output directory exists
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    data = load_existing()
    if "commodities" not in data or not isinstance(data.get("commodities"), dict):
        data["commodities"] = {}

    migrate_sugarcane_to_sugar(data)

    changed = False
    for key, entry in parsed.items():
        hist = data["commodities"].get(key)
        if not isinstance(hist, list):
            hist = []
        existing = next(
            (h for h in hist
             if h.get("year") == entry["year"] and h.get("month_code") == entry["month_code"]),
            None,
        )
        if existing and abs(float(existing.get("index", -1)) - entry["index"]) < 1e-9:
            data["commodities"][key] = hist
            print(f"  {key:15s} index {entry['index']:.2f}  (no change)")
            continue
        hist = [h for h in hist
                if not (h.get("year") == entry["year"] and h.get("month_code") == entry["month_code"])]
        hist.append(entry)
        hist.sort(key=lambda h: (h.get("year", ""), h.get("month_code", 0)))
        hist = hist[-120:]  # keep last 10 years monthly
        data["commodities"][key] = hist
        changed = True
        print(f"  {key:15s} index {entry['index']:.2f}  (updated)")

    if not changed:
        print(f"No CPI change since last run ({MONTHS[mcode]} {year} already stored). Nothing to commit.")
        return

    data["base_year"] = "2024"
    data["updated"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved CPI for {MONTHS[mcode]} {year} to {DATA_FILE}.")
    print("Done!")


if __name__ == "__main__":
    if "--discover" in sys.argv:
        discover_oils()
    else:
        main_collect()
