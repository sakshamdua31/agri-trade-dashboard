"""
Forex USD/INR Collector — frankfurter.app (ECB reference rates)
Free API, no key required, no CAPTCHA.

Modes:
  python3 collect_forex.py --backfill   → fetch monthly rates 2014-01 to now
  python3 collect_forex.py              → fetch latest rate (daily cron)

Writes to oilseeds_mir.json under macro.forex_usd_inr
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = "data/static/oilseeds_mir.json"
API_BASE = "https://api.frankfurter.app"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "AgriDashboard/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def backfill():
    """Fetch monthly USD/INR from 2014-01-01 to today."""
    print("Backfilling USD/INR monthly rates (2014 → now)...")

    # API allows max ~10 year range per call, so split into chunks
    all_rates = {}
    chunks = [
        ("2014-01-01", "2018-12-31"),
        ("2019-01-01", "2023-12-31"),
        ("2024-01-01", datetime.now(IST).strftime("%Y-%m-%d")),
    ]

    for start, end in chunks:
        print(f"  Fetching {start} → {end}...")
        url = f"{API_BASE}/{start}..{end}?to=INR"
        data = fetch_json(url)
        rates = data.get("rates", {})
        all_rates.update(rates)
        print(f"    Got {len(rates)} daily rates")

    # Convert to monthly: pick last available rate per month
    monthly = {}
    for date_str in sorted(all_rates.keys()):
        ym = date_str[:7]  # "2024-01"
        rate = all_rates[date_str].get("INR")
        if rate:
            monthly[ym] = rate  # last date in month wins

    # Convert to list format
    records = []
    for ym in sorted(monthly.keys()):
        year, month = ym.split("-")
        records.append({
            "year": int(year),
            "month": int(month),
            "rate": round(monthly[ym], 2)
        })

    print(f"  Monthly records: {len(records)} ({records[0]['year']}-{records[0]['month']:02d} → {records[-1]['year']}-{records[-1]['month']:02d})")
    return records


def fetch_latest():
    """Fetch today's USD/INR rate."""
    print("Fetching latest USD/INR rate...")
    data = fetch_json(f"{API_BASE}/latest?from=USD&to=INR")
    rate = data.get("rates", {}).get("INR")
    date = data.get("date", "")
    if rate:
        print(f"  {date}: ₹{rate}")
        year, month = date.split("-")[:2]
        return [{"year": int(year), "month": int(month), "rate": round(rate, 2), "date": date}]
    return []


def save(records, mode="backfill"):
    """Save to oilseeds_mir.json."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            mir = json.load(f)
    else:
        mir = {}

    if "macro" not in mir:
        mir["macro"] = {}

    if mode == "backfill":
        mir["macro"]["forex_usd_inr"] = {
            "source": "ECB via frankfurter.app",
            "unit": "INR per 1 USD",
            "frequency": "monthly (last trading day)",
            "data": records
        }
    else:
        # Daily: append/update latest month
        existing = mir.get("macro", {}).get("forex_usd_inr", {}).get("data", [])
        for rec in records:
            # Replace if same year+month exists
            existing = [e for e in existing if not (e["year"] == rec["year"] and e["month"] == rec["month"])]
            existing.append(rec)
        existing.sort(key=lambda x: (x["year"], x["month"]))
        if "forex_usd_inr" not in mir["macro"]:
            mir["macro"]["forex_usd_inr"] = {"source": "ECB via frankfurter.app", "unit": "INR per 1 USD", "data": []}
        mir["macro"]["forex_usd_inr"]["data"] = existing

    mir["updated"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    with open(DATA_FILE, "w") as f:
        json.dump(f, mir) if False else json.dump(mir, f, indent=2)

    print(f"  Saved to {DATA_FILE}")


def main():
    if "--backfill" in sys.argv:
        records = backfill()
        save(records, mode="backfill")
    else:
        records = fetch_latest()
        if records:
            save(records, mode="daily")
        else:
            print("  No rate available")

    print("Done!")


if __name__ == "__main__":
    main()
