"""
All-Markets Collector — data.gov.in AgMarknet.
Fetches EVERY reporting market for all 7 commodities, paginates to get the
complete list, applies the same cleaning as collect_prices.py (excludes
dal/retail/out-of-band — but NO statistical outlier removal, so every
legitimate market is kept), and stores in agri_markets.json organized by
commodity → date → [market records].

Rolling retention: keeps the last RETENTION_DAYS days per commodity.
The dashboard will read this file for the exhaustive paginated mandi table.

Runs twice daily via collect_markets.yml (offset 15 min after collect_prices
to avoid git push conflicts).

Usage:  python collect_markets.py
Env:    API_KEY  (data.gov.in key — falls back to sample key if unset)
"""

import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = "agri_markets.json"
RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
API_KEY = os.environ.get("API_KEY", "579b464db66ec23bdd000001e6c08e18ba004dd6537d5f85af1d3bfb")
RETENTION_DAYS = 30
FETCH_LIMIT = 500          # records per API call (paginate for more)

# ---- Same commodity config as collect_prices.py ----
COMMODITIES = {
    "paddy":     {"filter": "Paddy(Dhan)(Common)", "exclude_varieties": [],
                  "sane_min": 1000, "sane_max": 6000},
    "wheat":     {"filter": "Wheat", "exclude_varieties": [],
                  "sane_min": 1500, "sane_max": 5000},
    "maize":     {"filter": "Maize", "exclude_varieties": ["Popcorn"],
                  "sane_min": 800,  "sane_max": 4000},
    "sugarcane": {"filter": "Sugarcane", "exclude_varieties": [],
                  "sane_min": 150,  "sane_max": 600},
    "tur":       {"filter": "Arhar (Tur/Red Gram)(Whole)",
                  "exclude_varieties": ["Dal", "Black Gram", "Green Gram", "Bengal"],
                  "sane_min": 3500, "sane_max": 13000},
    "gram":      {"filter": "Bengal Gram(Gram)(Whole)",
                  "exclude_varieties": ["Dal", "Green", "Black", "Moong", "Urad", "Tur", "Arhar"],
                  "sane_min": 3500, "sane_max": 9000},
    "onion":     {"filter": "Onion", "exclude_varieties": [],
                  "sane_min": 100,  "sane_max": 10000},
}
EXCLUDE_MARKETS = ["Uzhavar Sandhai"]


# ---- API helpers ----

def fetch_page(commodity_filter, offset=0):
    """Fetch one page of records from data.gov.in."""
    params = urllib.parse.urlencode({
        "api-key": API_KEY,
        "format": "json",
        "limit": str(FETCH_LIMIT),
        "offset": str(offset),
        "filters[commodity]": commodity_filter,
    })
    url = f"https://api.data.gov.in/resource/{RESOURCE_ID}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "AgriDashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_all_records(commodity_filter):
    """Paginate through ALL records for a commodity."""
    all_records = []
    offset = 0
    while True:
        data = fetch_page(commodity_filter, offset)
        records = data.get("records", [])
        all_records.extend(records)
        total = int(data.get("total", 0))
        fetched = offset + len(records)
        if fetched >= total or not records:
            break
        offset += FETCH_LIMIT
    return all_records, total


# ---- Cleaning ----

def normalize_date(raw):
    """Convert 'DD/MM/YYYY' → 'YYYY-MM-DD'."""
    try:
        parts = raw.strip().split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    except Exception:
        pass
    return None


def clean_record(r, cfg):
    """Return a cleaned dict, or None if the record should be excluded."""
    try:
        modal = int(r.get("modal_price", 0))
        mn    = int(r.get("min_price", 0))
        mx    = int(r.get("max_price", 0))
    except (ValueError, TypeError):
        return None
    if modal <= 0:
        return None

    variety = (r.get("variety") or "").strip()
    for excl in cfg["exclude_varieties"]:
        if excl.lower() in variety.lower():
            return None

    market = (r.get("market") or "").strip()
    for excl in EXCLUDE_MARKETS:
        if excl.lower() in market.lower():
            return None

    if modal < cfg["sane_min"] or modal > cfg["sane_max"]:
        return None

    date = normalize_date(r.get("arrival_date", ""))
    if not date:
        return None

    return {
        "market": market,
        "state": (r.get("state") or "").strip(),
        "district": (r.get("district") or "").strip(),
        "variety": variety,
        "modal_price": modal,
        "min_price": mn,
        "max_price": mx,
        "arrival_date": date,
    }


# ---- Main ----

def main():
    now = datetime.now(IST)
    print(f"Collecting ALL markets at {now.strftime('%Y-%m-%d %H:%M IST')}...")

    # Load existing file
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}

    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    grand_total = 0

    for key, cfg in COMMODITIES.items():
        print(f"\n  {key} ({cfg['filter']})...")
        try:
            raw, api_total = fetch_all_records(cfg["filter"])
        except Exception as e:
            print(f"    FETCH ERROR: {e}")
            continue

        print(f"    API total: {api_total}, fetched: {len(raw)}")

        # Clean and group by arrival_date
        by_date = {}
        stats = {"variety": 0, "market": 0, "band": 0, "bad": 0}

        for r in raw:
            cleaned = clean_record(r, cfg)
            if cleaned:
                dt = cleaned["arrival_date"]
                by_date.setdefault(dt, []).append(cleaned)
            else:
                # Categorize exclusion for logging
                variety = (r.get("variety") or "")
                market  = (r.get("market") or "")
                try:
                    modal = int(r.get("modal_price", 0))
                except (ValueError, TypeError):
                    modal = 0

                if any(ex.lower() in variety.lower() for ex in cfg["exclude_varieties"]):
                    stats["variety"] += 1
                elif any(ex.lower() in market.lower() for ex in EXCLUDE_MARKETS):
                    stats["market"] += 1
                elif modal > 0 and (modal < cfg["sane_min"] or modal > cfg["sane_max"]):
                    stats["band"] += 1
                else:
                    stats["bad"] += 1

        clean_count = sum(len(v) for v in by_date.values())
        print(f"    cleaned: {clean_count} records across {len(by_date)} date(s)")
        print(f"    excluded: {stats}")

        # Merge into stored data
        if key not in data or not isinstance(data[key], dict):
            data[key] = {}

        commodity = data[key]

        # Remove metadata keys that aren't dates (safety)
        date_keys = [k for k in commodity if k >= "2000" and k <= "2099"]

        # Update with new dates (replace if same date, so latest fetch wins)
        for dt, records in by_date.items():
            commodity[dt] = records

        # Prune dates older than retention
        all_dates = sorted([k for k in commodity if k >= "2000" and k <= "2099"], reverse=True)
        for dt in all_dates:
            if dt < cutoff:
                del commodity[dt]
                print(f"    pruned: {dt}")

        kept_dates = sorted([k for k in commodity if k >= "2000" and k <= "2099"], reverse=True)
        total_records = sum(len(commodity[d]) for d in kept_dates)
        grand_total += total_records
        print(f"    stored: {total_records} records across {len(kept_dates)} date(s)")
        if kept_dates:
            print(f"    latest date: {kept_dates[0]}")

    # Metadata
    data["updated"] = now.strftime("%Y-%m-%d %H:%M:%S IST")
    data["retention_days"] = RETENTION_DAYS

    # Save
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)    # no indent — keeps file compact

    size_kb = os.path.getsize(DATA_FILE) / 1024
    print(f"\nTotal: {grand_total} market records in {DATA_FILE} ({size_kb:.0f} KB)")
    print("Done!")


if __name__ == "__main__":
    main()
