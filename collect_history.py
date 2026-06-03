"""
Historical Price Collector — CEDA Ashoka University AgMarknet API.
Fetches daily All-India mandi modal prices for all 7 commodities,
going back to 2011. Stores in agri_history.json.

First run = full backfill (2011-present).
Subsequent runs = incremental update (fetches last 2 years to catch
new CEDA monthly releases, merges with existing data).

CEDA updates monthly with ~2 month lag, so run this monthly.
Rate limit: 40 requests per hour (we use 7, one per commodity).

Usage:  python collect_history.py
Env:    CEDA_API_KEY  (Bearer token from api.ceda.ashoka.edu.in)

Credit: "CEDA Agri Market Data, Centre for Economic Data & Analysis,
         Ashoka University" — required by CEDA terms of use.
"""

import json
import os
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = "agri_history.json"
BASE_URL = "https://api.ceda.ashoka.edu.in/v1/agmarknet/prices"
API_KEY = os.environ.get("CEDA_API_KEY") or "634aca8998a867283e2dfd476170b774f63fca1af314d103634585a712cb309f"

STATE_ID = 0                # All India
BACKFILL_FROM = "2011-01-01" # CEDA has data from ~2000; 2011 matches UPAg range
INCREMENTAL_YEARS = 2        # For updates, re-fetch last N years
CALL_DELAY = 3               # Seconds between API calls (rate limit courtesy)

COMMODITIES = {
    "wheat":     {"id": 1,   "name": "Wheat"},
    "paddy":     {"id": 2,   "name": "Paddy(Dhan)(Common)"},
    "maize":     {"id": 4,   "name": "Maize"},
    "gram":      {"id": 6,   "name": "Bengal Gram(Gram)(Whole)"},
    "onion":     {"id": 23,  "name": "Onion"},
    "tur":       {"id": 49,  "name": "Arhar (Tur/Red Gram)(Whole)"},
    "sugarcane": {"id": 150, "name": "Sugarcane"},
}


def fetch_prices(commodity_id, from_date, to_date):
    """POST to CEDA /agmarknet/prices, return list of price records."""
    body = json.dumps({
        "commodity_id": commodity_id,
        "state_id": STATE_ID,
        "from_date": from_date,
        "to_date": to_date,
    }).encode("utf-8")

    req = urllib.request.Request(
        BASE_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "ArcusAgriDashboard/1.0",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        data = json.loads(resp.read().decode())

    if data.get("output", {}).get("type") != "success":
        msg = data.get("output", {}).get("message", "Unknown error")
        raise RuntimeError(f"API error: {msg}")

    return data["output"]["data"]


def process_records(raw_records):
    """Convert API records to compact {date, modal, min, max}, deduplicated."""
    by_date = {}
    for r in raw_records:
        d = r["date"][:10]
        by_date[d] = {
            "date": d,
            "modal": round(r["modal_price"], 1),
            "min": round(r["min_price"], 1),
            "max": round(r["max_price"], 1),
        }
    return [by_date[k] for k in sorted(by_date)]


def merge_records(existing, new_records):
    """Merge new into existing, new overwrites same-date entries."""
    by_date = {r["date"]: r for r in existing}
    for r in new_records:
        by_date[r["date"]] = r
    return [by_date[k] for k in sorted(by_date)]


def main():
    now = datetime.now(IST)
    print(f"CEDA Historical Price Collector")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M IST')}")

    if not API_KEY:
        print("ERROR: CEDA_API_KEY not set.")
        print("Get your key at https://api.ceda.ashoka.edu.in/")
        return

    # Load existing or start fresh
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        is_backfill = False
        print(f"Loaded existing {DATA_FILE} — incremental update")
    else:
        data = {}
        is_backfill = True
        print(f"No {DATA_FILE} — full backfill")

    from_date = BACKFILL_FROM if is_backfill else (now - timedelta(days=INCREMENTAL_YEARS * 365)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")
    print(f"Date range: {from_date} to {to_date}\n")

    total = 0
    for key, cfg in COMMODITIES.items():
        print(f"  {key} (id={cfg['id']})...")
        try:
            raw = fetch_prices(cfg["id"], from_date, to_date)
            records = process_records(raw)
            print(f"    fetched: {len(records)} daily records")

            if key in data and isinstance(data[key], list) and not is_backfill:
                data[key] = merge_records(data[key], records)
                print(f"    merged → {len(data[key])} total")
            else:
                data[key] = records

            total += len(data[key])
            if records:
                print(f"    range: {records[0]['date']} → {records[-1]['date']}")
                print(f"    latest: Rs {records[-1]['modal']}/qtl")

        except Exception as e:
            print(f"    ERROR: {e}")
            if key in data and isinstance(data[key], list):
                total += len(data[key])
                print(f"    keeping {len(data[key])} existing records")

        time.sleep(CALL_DELAY)

    data["updated"] = now.strftime("%Y-%m-%d %H:%M:%S IST")
    data["source"] = "CEDA Agri Market Data, Centre for Economic Data & Analysis, Ashoka University"
    data["credit"] = "https://ceda.ashoka.edu.in/api-terms-conditions/"
    data["from_date"] = BACKFILL_FROM
    data["state"] = "All India"

    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

    size_kb = os.path.getsize(DATA_FILE) / 1024
    print(f"\nTotal: {total} records across {len(COMMODITIES)} commodities")
    print(f"Saved: {DATA_FILE} ({size_kb:.0f} KB)")
    print("Done!")


if __name__ == "__main__":
    main()
