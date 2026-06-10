"""
seed_cpi_history.py — One-time script to fetch historical CPI data from MoSPI eSankhyiki API.

Fetches monthly CPI indices for all 7 dashboard commodities going back ~3 years.
Stores in agri_history.json alongside mandi/retail data.

Usage:  python seed_cpi_history.py
Run from the repo root (same folder as agri_history.json).

No authentication needed — public API.
"""

import ssl
import json
import os
import urllib.parse
import urllib.request
import time
from datetime import datetime, timezone, timedelta

HISTORY_FILE = "agri_history.json"
BASE = "https://api.mospi.gov.in"

# Commodity key -> CPI item_code (base year 2024)
ITEM_CODES = {
    "paddy": 1,       # Rice
    "wheat": 2,        # Wheat
    "maize": 6,        # Maize and its products
    "onion": 92,       # Onion
    "tur": 99,         # Arhar, tur
    "gram": 104,       # Gram: whole
    "sugarcane": 111,  # Sugar
}

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
        print(f"    Error: {e}")
        return []


def main():
    print("\n═══ Seed CPI History ═══\n")

    # Load existing history
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
    else:
        history = {}

    # Ensure cpi_history key exists for each commodity
    for key in ITEM_CODES:
        if key not in history:
            history[key] = []

    # Generate list of (year, month) to fetch — last 36 months
    now = datetime.now()
    months_to_fetch = []
    for i in range(36):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        months_to_fetch.append((y, m))

    months_to_fetch.reverse()  # oldest first

    total_added = 0
    total_skipped = 0

    for y, m in months_to_fetch:
        month_str = f"{y}-{m:02d}"
        print(f"  Fetching {MONTHS[m]} {y}...", end=" ")

        rows = fetch_month(y, m)
        if not rows:
            print("no data")
            time.sleep(0.5)
            continue

        month_count = 0
        for row in rows:
            name = (row.get("item") or "").strip().lower()
            key = NAME_TO_KEY.get(name)
            if not key:
                continue
            try:
                idx_val = float(row.get("index"))
            except (TypeError, ValueError):
                continue

            # Store as a CPI entry in the commodity's history
            # Use the last day of the month as the date
            if m == 12:
                last_day = 31
            elif m in [4, 6, 9, 11]:
                last_day = 30
            elif m == 2:
                last_day = 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28
            else:
                last_day = 31

            date_str = f"{y}-{m:02d}-{last_day:02d}"

            # Check if this date already has CPI data
            existing = next((r for r in history[key] if r.get("date") == date_str), None)
            if existing:
                if "cpi" in existing and abs(existing["cpi"] - idx_val) < 0.01:
                    total_skipped += 1
                    continue
                existing["cpi"] = idx_val
                existing["cpi_month"] = MONTHS[m]
                existing["cpi_year"] = y
            else:
                history[key].append({
                    "date": date_str,
                    "cpi": idx_val,
                    "cpi_month": MONTHS[m],
                    "cpi_year": y
                })

            month_count += 1
            total_added += 1

        print(f"{month_count} items")
        time.sleep(0.3)  # Be nice to the API

    # Re-sort all commodity arrays
    for key in ITEM_CODES:
        history[key].sort(key=lambda r: r["date"])

    # Summary
    print(f"\n  Total added/updated: {total_added}, skipped (unchanged): {total_skipped}")
    print("\n  CPI coverage per commodity:")
    for key in sorted(ITEM_CODES.keys()):
        cpi_records = [r for r in history[key] if "cpi" in r]
        if cpi_records:
            print(f"    {key}: {cpi_records[0]['date']} → {cpi_records[-1]['date']} ({len(cpi_records)} months)")
        else:
            print(f"    {key}: no CPI data")

    # Save
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)
    print(f"\n  Saved to {HISTORY_FILE}")
    print("\n═══ Done ═══\n")


if __name__ == "__main__":
    main()
