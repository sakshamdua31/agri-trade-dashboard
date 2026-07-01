"""
Daily Commodity Price Collector — All 11 Commodities
Calls the data.gov.in Agmarknet API for each commodity,
and appends daily summaries to agri_data.json.

Usage:
  python3 collect_prices.py

Output:
  agri_data.json — grows by one entry per commodity per session
"""

import json
import os
import time
import statistics
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# --- Configuration ---
API_KEY = os.environ.get("API_KEY", "")
RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
DATA_FILE = "agri_data.json"

# Commodity config: API filter name + data-cleaning rules
#  - exclude_varieties: drop any record whose variety contains these (case-insensitive).
#    Used to remove processed "Dal" and wrong pulses that contaminate whole-grain averages.
#  - sane_min / sane_max: drop modal prices outside this band (contamination backstop).
#    Set generously so real price swings are kept but gross errors (e.g. dal in gram) are dropped.
COMMODITIES = {
    "paddy": {
        "filter": "Paddy(Dhan)(Common)",
        "exclude_varieties": [],
        "sane_min": 1000, "sane_max": 6000
    },
    "wheat": {
        "filter": "Wheat",
        "exclude_varieties": [],
        "sane_min": 1500, "sane_max": 5000
    },
    "maize": {
        "filter": "Maize",
        "exclude_varieties": ["Popcorn"],
        "sane_min": 800, "sane_max": 4000
    },
    "sugarcane": {
        "filter": "Sugarcane",
        "exclude_varieties": [],
        "sane_min": 150, "sane_max": 600
    },
    "tur": {
        "filter": "Arhar (Tur/Red Gram)(Whole)",
        "exclude_varieties": ["Dal", "Black Gram", "Green Gram", "Bengal"],
        "sane_min": 3500, "sane_max": 13000
    },
    "gram": {
        "filter": "Bengal Gram(Gram)(Whole)",
        "exclude_varieties": ["Dal", "Green", "Black", "Moong", "Urad", "Tur", "Arhar"],
        "sane_min": 3500, "sane_max": 9000
    },
    "onion": {
        "filter": "Onion",
        "exclude_varieties": [],
        "sane_min": 100, "sane_max": 10000
    },
    "soybean": {
        "filter": "Soyabean",
        "exclude_varieties": [],
        "sane_min": 2000, "sane_max": 8000
    },
    "groundnut": {
        "filter": "Groundnut",
        "exclude_varieties": ["Oil"],
        "sane_min": 3000, "sane_max": 12000
    },
    "sunflower": {
        "filter": "Sunflower",
        "exclude_varieties": ["Oil"],
        "sane_min": 3000, "sane_max": 10000
    },
    "mustard": {
        "filter": "Mustard",
        "exclude_varieties": [],
        "sane_min": 3000, "sane_max": 9000
    }
}

# Retail farmer markets that report consumer-level (not wholesale) prices — exclude.
EXCLUDE_MARKETS = ["Uzhavar Sandhai"]

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))


def fetch_prices(commodity_filter):
    """Call data.gov.in API for a commodity. Retries on rate limit."""
    params = urllib.parse.urlencode({
        "api-key": API_KEY,
        "format": "json",
        "limit": 200,
        "filters[commodity]": commodity_filter
    })
    url = f"https://api.data.gov.in/resource/{RESOURCE_ID}?{params}"

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AgriDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            records = data.get("records", [])
            if not records:
                return None
            return records

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 30 * (attempt + 1)
                print(f"  Rate limited. Waiting {wait}s before retry {attempt + 2}/4...")
                time.sleep(wait)
            else:
                raise

    return None


def process_records(records, commodity_key):
    """Clean records and compute daily summary with contamination + outlier removal."""
    conf = COMMODITIES[commodity_key]
    exclude_varieties = [v.lower() for v in conf.get("exclude_varieties", [])]
    sane_min = conf.get("sane_min", 0)
    sane_max = conf.get("sane_max", 10**9)

    clean = []
    excluded_variety = 0   # dropped as wrong variety / processed dal
    excluded_market = 0    # dropped as retail farmer market
    excluded_band = 0      # dropped as outside sane price band
    for r in records:
        try:
            modal = int(float(r.get("modal_price", 0)))
        except (ValueError, TypeError):
            continue
        if modal <= 0:
            continue

        variety = r.get("variety", "")
        market = r.get("market", "")

        # 1. Drop retail farmer markets (consumer-level, not wholesale)
        if any(ex.lower() in market.lower() for ex in EXCLUDE_MARKETS):
            excluded_market += 1
            continue
        # 2. Drop processed dal / wrong-pulse varieties
        if any(ev in variety.lower() for ev in exclude_varieties):
            excluded_variety += 1
            continue
        # 3. Drop prices outside the sane band (gross contamination backstop)
        if modal < sane_min or modal > sane_max:
            excluded_band += 1
            continue

        clean.append({
            "market": market,
            "state": r.get("state", ""),
            "district": r.get("district", ""),
            "variety": variety,
            "min_price": int(float(r.get("min_price", 0))),
            "max_price": int(float(r.get("max_price", 0))),
            "modal_price": modal,
            "arrival_date": r.get("arrival_date", "")
        })

    if not clean:
        return None

    # 4. Statistical outlier removal via IQR (only with enough data points)
    outliers_removed = 0
    if len(clean) >= 8:
        modals_all = sorted(r["modal_price"] for r in clean)
        q1, _, q3 = statistics.quantiles(modals_all, n=4)
        iqr = q3 - q1
        lo = q1 - 1.5 * iqr
        hi = q3 + 1.5 * iqr
        filtered = [r for r in clean if lo <= r["modal_price"] <= hi]
        outliers_removed = len(clean) - len(filtered)
        if filtered:  # never wipe everything
            clean = filtered

    modals = [r["modal_price"] for r in clean]
    avg_price = round(statistics.mean(modals))
    modal_price = round(statistics.median(modals))

    top_markets = sorted(clean, key=lambda x: x["modal_price"], reverse=True)[:8]

    now = datetime.now(IST)
    session = "morning" if now.hour < 15 else "evening"

    return {
        "date": now.strftime("%Y-%m-%d"),
        "session": session,
        "collected_at": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "commodity": commodity_key,
        "total_markets": len(clean),
        "avg_price": avg_price,
        "modal_price": modal_price,
        "max_price": max(modals),
        "min_price": min(modals),
        "cleaning": {
            "excluded_variety": excluded_variety,
            "excluded_market": excluded_market,
            "excluded_band": excluded_band,
            "outliers_removed": outliers_removed
        },
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


def save_to_file(entries):
    """Load existing data, append all entries, save back."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {"updated": ""}

    # Ensure config section exists with MSP defaults (user edits these in GitHub)
    if "config" not in data:
        data["config"] = {
            "msp": {
                "paddy":     {"value": 2300, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "wheat":     {"value": 2425, "unit": "/qtl", "season": "RMS 2025-26", "effective": "2025-04-01"},
                "maize":     {"value": 2090, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "sugarcane": {"value": 340,  "unit": "/qtl", "season": "2025-26",     "effective": "2025-10-01"},
                "tur":       {"value": 7550, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "gram":      {"value": 5650, "unit": "/qtl", "season": "RMS 2025-26", "effective": "2025-04-01"},
                "onion":     {"value": 0,    "unit": "/qtl", "season": "N/A",         "effective": "N/A"},
                "groundnut": {"value": 6783, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "sunflower": {"value": 7280, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "soybean":   {"value": 4892, "unit": "/qtl", "season": "KMS 2025-26", "effective": "2025-10-01"},
                "mustard":   {"value": 5950, "unit": "/qtl", "season": "RMS 2025-26", "effective": "2025-04-01"}
            }
        }

    for entry in entries:
        key = entry["commodity"]

        # Ensure structure exists for this commodity
        if key not in data:
            data[key] = {"history": []}
        if "history" not in data[key]:
            data[key]["history"] = []

        history = data[key]["history"]

        # Replace same date+session entry if exists
        today = entry["date"]
        session = entry["session"]
        history = [h for h in history if not (h["date"] == today and h.get("session") == session)]
        history.append(entry)

        # Keep last 365 days max (730 entries with 2 sessions/day)
        history = history[-730:]

        data[key]["history"] = history

    data["updated"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def main():
    now = datetime.now(IST)
    print(f"Collecting prices for 11 commodities at {now.strftime('%Y-%m-%d %H:%M IST')}...")
    print("=" * 60)

    entries = []

    for key, conf in COMMODITIES.items():
        api_name = conf["filter"]
        print(f"\n  {key.upper()} ({api_name})...")

        records = fetch_prices(api_name)
        if not records:
            print(f"  ✗ No data returned for {key}")
            continue

        entry = process_records(records, key)
        if not entry:
            print(f"  ✗ No valid prices for {key}")
            continue

        entries.append(entry)
        cl = entry["cleaning"]
        dropped = cl["excluded_variety"] + cl["excluded_market"] + cl["excluded_band"] + cl["outliers_removed"]
        print(f"  ✓ Avg: ₹{entry['avg_price']} | Modal: ₹{entry['modal_price']} | Markets: {entry['total_markets']} | Dropped: {dropped}")

        # Wait 5 seconds between API calls to avoid rate limiting
        time.sleep(5)

    if entries:
        save_to_file(entries)
        print(f"\n{'=' * 60}")
        print(f"Saved {len(entries)}/11 commodities to {DATA_FILE}")
    else:
        print("\nFAILED: No data collected for any commodity")

    print("Done!")


if __name__ == "__main__":
    main()
