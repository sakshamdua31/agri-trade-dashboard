"""
Daily Paddy Price Collector
Calls the data.gov.in Agmarknet API, collects mandi prices for Paddy,
and appends a daily summary to agri_data.json.

Usage:
  python3 collect_paddy_prices.py

Output:
  agri_data.json — grows by one entry per day
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# --- Configuration ---
API_KEY = "579b464db66ec23bdd000001e6c08e18ba004dd6537d5f85af1d3bfb"
RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
COMMODITY = "Paddy(Dhan)(Common)"
DATA_FILE = "agri_data.json"

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))


def fetch_prices():
    """Call data.gov.in API and return raw records. Retries on rate limit."""
    import time

    params = urllib.parse.urlencode({
        "api-key": API_KEY,
        "format": "json",
        "limit": 200,
        "filters[commodity]": COMMODITY
    })
    url = f"https://api.data.gov.in/resource/{RESOURCE_ID}?{params}"

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AgriDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            records = data.get("records", [])
            if not records:
                print("WARNING: API returned 0 records")
                return None
            return records

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 30 * (attempt + 1)
                print(f"Rate limited. Waiting {wait}s before retry {attempt + 2}/4...")
                time.sleep(wait)
            else:
                raise

    return None


def process_records(records):
    """Clean records and compute daily summary."""
    clean = []
    for r in records:
        modal = 0
        try:
            modal = int(float(r.get("modal_price", 0)))
        except (ValueError, TypeError):
            continue
        if modal <= 0:
            continue
        clean.append({
            "market": r.get("market", ""),
            "state": r.get("state", ""),
            "district": r.get("district", ""),
            "variety": r.get("variety", ""),
            "min_price": int(float(r.get("min_price", 0))),
            "max_price": int(float(r.get("max_price", 0))),
            "modal_price": modal,
            "arrival_date": r.get("arrival_date", "")
        })

    if not clean:
        print("WARNING: No valid price records after cleaning")
        return None

    modals = [r["modal_price"] for r in clean]
    avg_price = round(sum(modals) / len(modals))
    max_price = max(modals)
    min_price = min(modals)

    # Top 8 markets by modal price
    top_markets = sorted(clean, key=lambda x: x["modal_price"], reverse=True)[:8]

    now = datetime.now(IST)
    session = "morning" if now.hour < 15 else "evening"

    return {
        "date": now.strftime("%Y-%m-%d"),
        "session": session,
        "collected_at": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "commodity": "paddy",
        "total_markets": len(clean),
        "avg_price": avg_price,
        "max_price": max_price,
        "min_price": min_price,
        "top_markets": [
            {
                "market": m["market"],
                "state": m["state"],
                "variety": m["variety"],
                "modal_price": m["modal_price"],
                "min_price": m["min_price"],
                "max_price": m["max_price"]
            }
            for m in top_markets
        ],
        "arrival_date": clean[0].get("arrival_date", "")
    }


def save_to_file(entry):
    """Load existing data, append today's entry, save back."""
    # Load existing file or start fresh
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {"updated": "", "paddy": {"history": []}}

    # Ensure structure exists
    if "paddy" not in data:
        data["paddy"] = {"history": []}
    if "history" not in data["paddy"]:
        data["paddy"]["history"] = []

    history = data["paddy"]["history"]

    # Check if this session's entry already exists — replace it
    today = entry["date"]
    session = entry["session"]
    history = [h for h in history if not (h["date"] == today and h.get("session") == session)]
    history.append(entry)

    # Keep last 365 days max
    history = history[-365:]

    data["paddy"]["history"] = history
    data["updated"] = entry["collected_at"]

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Saved: {today} | Avg: ₹{entry['avg_price']} | Markets: {entry['total_markets']}")


def main():
    print(f"Collecting Paddy prices at {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}...")

    records = fetch_prices()
    if not records:
        print("FAILED: Could not fetch data from API")
        return

    entry = process_records(records)
    if not entry:
        print("FAILED: No valid data to save")
        return

    save_to_file(entry)
    print("Done!")


if __name__ == "__main__":
    main()
