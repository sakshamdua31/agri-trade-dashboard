"""
Monthly CPI Collector — MOSPI eSankhyiki (base year 2024, All-India, Combined).
Fetches item-level CPI Index + Inflation for the 7 dashboard commodities and
appends them to agri_data.json under a "cpi" section.

No authentication required — public GET endpoints.

Usage:  python collect_cpi.py
"""

import ssl
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DATA_FILE = "agri_data.json"
BASE = "https://api.mospi.gov.in"
IST = timezone(timedelta(hours=5, minutes=30))

# commodity key -> CPI item_code (base year 2024)
ITEM_CODES = {
    "paddy": 1,    # Rice
    "wheat": 2,    # Wheat
    "maize": 6,    # Maize and its products
    "onion": 92,   # Onion
    "tur": 99,     # Arhar, tur
    "gram": 104,   # Gram: whole
    "sugarcane": 111,  # Sugar
}
# CPI item name (as returned by API, lowercased) -> commodity key
NAME_TO_KEY = {
    "rice": "paddy",
    "wheat": "wheat",
    "maize and its products": "maize",
    "onion": "onion",
    "arhar, tur": "tur",
    "gram: whole": "gram",
    "sugar": "sugarcane",
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


def fetch_month(year, month_code):
    """Fetch all 7 items for one (year, month). Returns list of rows or []."""
    codes = ",".join(str(c) for c in ITEM_CODES.values())
    try:
        j = get("/api/cpi/getCPIData", {
            "base_year": "2024", "series": "Current", "level": "Item",
            "state_code": "1", "sector_code": "3",
            "item_code": codes, "year": str(year), "month_code": str(month_code),
            "Format": "JSON", "limit": "50"
        })
        return j.get("data", []) or []
    except Exception as e:
        print(f"  fetch error for {year}-{month_code}: {e}")
        return []


def candidate_months():
    """Most recent months first, going back ~5 months from today."""
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


def main():
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
            "source": "MOSPI eSankhyiki (All-India, Combined)"
        }

    if not parsed:
        print("FAILED: could not match any items to commodities")
        return

    # Load existing data file and merge
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}

    if "cpi" not in data or not isinstance(data.get("cpi"), dict):
        data["cpi"] = {}

    changed = False
    for key, entry in parsed.items():
        hist = data["cpi"].get(key)
        if not isinstance(hist, list):
            hist = []
        # Is this exact month already stored with the same index? -> no change
        existing = next((h for h in hist
                         if h.get("year") == entry["year"] and h.get("month_code") == entry["month_code"]), None)
        if existing and abs(float(existing.get("index", -1)) - entry["index"]) < 1e-9:
            data["cpi"][key] = hist  # unchanged, keep as-is
            print(f"  {key:10s} index {entry['index']:.2f}  (no change)")
            continue
        # New month, or a revised index for the same month -> update
        hist = [h for h in hist if not (h.get("year") == entry["year"] and h.get("month_code") == entry["month_code"])]
        hist.append(entry)
        hist.sort(key=lambda h: (h.get("year", ""), h.get("month_code", 0)))
        hist = hist[-120:]  # keep last 10 years monthly
        data["cpi"][key] = hist
        changed = True
        print(f"  {key:10s} index {entry['index']:.2f}  (updated)")

    if not changed:
        print(f"No CPI change since last run ({MONTHS[mcode]} {year} already stored). Nothing to commit.")
        return

    data["cpi"]["base_year"] = "2024"
    data["cpi"]["updated"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved CPI for {MONTHS[mcode]} {year}.")
    print("Done!")


if __name__ == "__main__":
    main()
