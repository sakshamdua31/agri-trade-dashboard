"""
NCDEX Futures Data Collector
=============================
Fetches daily settlement prices (bhavcopy) from NCDEX for your 6 commodities.
NCDEX is a private exchange — NOT NIC-hosted — so it should work from GitHub Actions.

Usage:
  python collect_futures.py                    # today's data
  python collect_futures.py --date 2026-07-28  # specific date

Output:
  data/futures/futures_latest.json     → today's snapshot
  data/futures/futures_history.json    → appending daily history
"""

import requests
import json
import os
import sys
from datetime import datetime, timedelta
import argparse

# ── Configuration ──────────────────────────────────────────────
OUTPUT_DIR = "data/futures"
LATEST_FILE = os.path.join(OUTPUT_DIR, "futures_latest.json")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "futures_history.json")

# Your commodities → NCDEX symbol mapping
COMMODITIES = {
    "Chana":    "CHANA",
    "Soybean":  "SYBEANIDR",
    "Wheat":    "WHEATFAQ",
    "Maize":    "MAIZE",
    "CPO":      "CPO",          # Crude Palm Oil
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_bhavcopy_csv(session, date_str):
    """
    Try downloading NCDEX bhavcopy CSV.
    The URL pattern may vary — we try multiple known patterns.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")

    # Pattern 1: UDiFF format (from July 2024 onwards)
    # Common pattern: /downloads/bhavcopy/...
    urls_to_try = [
        f"https://www.ncdex.com/downloads/bhavcopy/csv/{dt.strftime('%d%m%Y')}.csv",
        f"https://www.ncdex.com/Downloads/Markets/BhavCopy/csv/ncdex_fut_{dt.strftime('%d%m%Y')}.csv",
        f"https://www.ncdex.com/downloads/bhavcopy/{dt.strftime('%d%m%Y')}_fut.csv",
    ]

    for url in urls_to_try:
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200 and len(r.text) > 100 and "," in r.text:
                print(f"  Bhavcopy CSV found at: {url}")
                return r.text, "csv"
        except Exception:
            continue

    return None, None


def fetch_via_api(session, date_str):
    """
    Try NCDEX API endpoints that the website uses internally.
    These return JSON and are more reliable than CSV downloads.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    formatted = dt.strftime("%d/%m/%Y")

    # Try the bhavcopy API endpoint
    api_urls = [
        {
            "url": "https://www.ncdex.com/api/MarketData/GetBhavCopySummary",
            "method": "POST",
            "data": {"date": formatted, "instrumentType": "Futures"}
        },
        {
            "url": "https://www.ncdex.com/MarketData/GetBhavCopySummary",
            "method": "POST",
            "data": {"date": formatted, "instrumentType": "Futures"}
        },
        {
            "url": f"https://www.ncdex.com/api/bhavcopy?date={formatted}&type=futures",
            "method": "GET",
            "data": None
        },
    ]

    for api in api_urls:
        try:
            if api["method"] == "POST":
                r = session.post(api["url"], json=api["data"], headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.ncdex.com/markets/bhavcopy",
                }, timeout=20)
            else:
                r = session.get(api["url"], headers=HEADERS, timeout=20)

            if r.status_code == 200:
                try:
                    data = r.json()
                    if data:
                        print(f"  API endpoint found: {api['url']}")
                        return data, "json"
                except Exception:
                    pass
        except Exception:
            continue

    return None, None


def fetch_historical_prices(session, symbol, date_str):
    """
    Try fetching from NCDEX historical futures prices page.
    This page has an API that returns contract-wise data.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    from_date = dt.strftime("%d/%m/%Y")
    to_date = from_date

    api_urls = [
        {
            "url": "https://www.ncdex.com/api/MarketData/GetFuturePrice",
            "data": {"symbol": symbol, "fromDate": from_date, "toDate": to_date}
        },
        {
            "url": "https://www.ncdex.com/MarketData/GetFuturePrice",
            "data": {"symbol": symbol, "fromDate": from_date, "toDate": to_date}
        },
    ]

    for api in api_urls:
        try:
            r = session.post(api["url"], json=api["data"], headers={
                **HEADERS,
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.ncdex.com/markets/futureprices",
            }, timeout=20)

            if r.status_code == 200:
                try:
                    data = r.json()
                    if data:
                        return data
                except Exception:
                    pass
        except Exception:
            continue

    return None


def parse_bhavcopy_csv(csv_text, date_str):
    """Parse bhavcopy CSV and extract data for our commodities."""
    lines = csv_text.strip().split("\n")
    if len(lines) < 2:
        return []

    # Get headers (first line)
    headers = [h.strip().lower().replace('"', '') for h in lines[0].split(",")]

    # Find relevant columns
    symbol_col = None
    for i, h in enumerate(headers):
        if "symbol" in h or "commodity" in h or "contract" in h:
            symbol_col = i
            break

    if symbol_col is None:
        print(f"  WARNING: Could not find symbol column. Headers: {headers}")
        return []

    # Our target symbols (uppercase)
    target_symbols = set(COMMODITIES.values())

    records = []
    for line in lines[1:]:
        cols = [c.strip().replace('"', '') for c in line.split(",")]
        if len(cols) <= symbol_col:
            continue

        symbol = cols[symbol_col].upper()

        # Check if this row matches any of our commodities
        matched_commodity = None
        for name, sym in COMMODITIES.items():
            if sym in symbol or symbol.startswith(sym[:5]):
                matched_commodity = name
                break

        if not matched_commodity:
            continue

        # Extract OHLC data (try common column positions)
        record = {
            "date": date_str,
            "commodity": matched_commodity,
            "ncdex_symbol": symbol,
            "raw_cols": {headers[i]: cols[i] for i in range(min(len(headers), len(cols)))},
        }

        # Try to extract numeric fields
        for i, h in enumerate(headers):
            if i < len(cols):
                try:
                    val = float(cols[i].replace(",", ""))
                except (ValueError, TypeError):
                    val = None

                if "open" in h:
                    record["open"] = val
                elif "high" in h:
                    record["high"] = val
                elif "low" in h:
                    record["low"] = val
                elif "close" in h or "settle" in h:
                    record["close"] = val
                elif "volume" in h or "traded_qty" in h:
                    record["volume"] = val
                elif "oi" in h or "open_interest" in h or "open interest" in h:
                    record["open_interest"] = val

        records.append(record)

    return records


def save_results(records, date_str):
    """Save futures data to JSON files."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Save latest snapshot
    latest = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": date_str,
        "source": "NCDEX",
        "contracts": records,
    }
    with open(LATEST_FILE, "w") as f:
        json.dump(latest, f, indent=2, ensure_ascii=False)
    print(f"  Saved → {LATEST_FILE}")

    # 2. Append to history
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            history = json.load(f)

    # Remove existing entries for this date
    history = [r for r in history if r.get("date") != date_str]
    history.extend(records)
    history.sort(key=lambda x: (x.get("date", ""), x.get("commodity", "")))

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    print(f"  Saved → {HISTORY_FILE} ({len(records)} contracts)")


def main():
    parser = argparse.ArgumentParser(description="Fetch NCDEX futures data")
    parser.add_argument("--date", type=str, default=None, help="Date (YYYY-MM-DD)")
    parser.add_argument("--test", action="store_true", help="Test connectivity only")
    args = parser.parse_args()

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")

    session = requests.Session()

    # Test mode: just check if NCDEX is reachable
    if args.test:
        print("Testing NCDEX connectivity...")
        try:
            r = session.get("https://www.ncdex.com/markets/bhavcopy",
                          headers=HEADERS, timeout=15)
            print(f"  Status: {r.status_code}")
            print(f"  Size: {len(r.text)} bytes")
            if r.status_code == 200:
                print("  ✓ NCDEX is ACCESSIBLE from this environment!")
            else:
                print(f"  ✗ NCDEX returned {r.status_code}")
                print(f"  Response: {r.text[:200]}")
        except Exception as e:
            print(f"  ✗ Connection failed: {e}")

        print("\nTesting MCX connectivity...")
        try:
            r = session.get("https://www.mcxindia.com/market-data/bhavcopy",
                          headers=HEADERS, timeout=15)
            print(f"  Status: {r.status_code}")
            if r.status_code == 200:
                print("  ✓ MCX is ACCESSIBLE!")
            else:
                print(f"  ✗ MCX returned {r.status_code}")
        except Exception as e:
            print(f"  ✗ Connection failed: {e}")
        return

    print(f"NCDEX Futures Collector")
    print(f"{'=' * 40}")
    print(f"Date: {target_date}")
    print(f"Commodities: {', '.join(COMMODITIES.keys())}")
    print()

    all_records = []

    # Method 1: Try bhavcopy CSV download
    print("Method 1: Trying bhavcopy CSV download...")
    csv_text, fmt = fetch_bhavcopy_csv(session, target_date)
    if csv_text:
        records = parse_bhavcopy_csv(csv_text, target_date)
        if records:
            all_records = records
            print(f"  Found {len(records)} contracts")

    # Method 2: Try API endpoint
    if not all_records:
        print("Method 2: Trying NCDEX API endpoint...")
        data, fmt = fetch_via_api(session, target_date)
        if data:
            print(f"  API returned data (type: {type(data).__name__})")
            # Parse based on response structure
            if isinstance(data, list):
                for item in data:
                    symbol = str(item.get("Symbol", item.get("symbol", "")))
                    for name, sym in COMMODITIES.items():
                        if sym in symbol.upper():
                            all_records.append({
                                "date": target_date,
                                "commodity": name,
                                "ncdex_symbol": symbol,
                                "open": item.get("Open", item.get("open")),
                                "high": item.get("High", item.get("high")),
                                "low": item.get("Low", item.get("low")),
                                "close": item.get("Close", item.get("close", item.get("SettlementPrice"))),
                                "volume": item.get("Volume", item.get("volume", item.get("TradedQty"))),
                                "open_interest": item.get("OI", item.get("oi", item.get("OpenInterest"))),
                            })

    # Method 3: Try historical prices per commodity
    if not all_records:
        print("Method 3: Trying per-commodity historical prices...")
        for name, symbol in COMMODITIES.items():
            print(f"  Fetching {name} ({symbol})...", end=" ", flush=True)
            data = fetch_historical_prices(session, symbol, target_date)
            if data:
                print("found data")
                if isinstance(data, list):
                    for item in data:
                        all_records.append({
                            "date": target_date,
                            "commodity": name,
                            "ncdex_symbol": symbol,
                            "close": item.get("Close", item.get("close", item.get("SettlementPrice"))),
                            "volume": item.get("Volume", item.get("volume")),
                            "open_interest": item.get("OI", item.get("oi")),
                        })
                elif isinstance(data, dict):
                    all_records.append({
                        "date": target_date,
                        "commodity": name,
                        "ncdex_symbol": symbol,
                        "raw_data": data,
                    })
            else:
                print("no data")

    # Save results
    if all_records:
        save_results(all_records, target_date)
        print(f"\n{'=' * 40}")
        print(f"Success! {len(all_records)} contracts saved.")
    else:
        print(f"\n{'=' * 40}")
        print("No data retrieved. Possible reasons:")
        print("  1. Market holiday (NCDEX closed on weekends)")
        print("  2. NCDEX blocking this IP (run --test to check)")
        print("  3. Date too recent (bhavcopy uploads after 6 PM IST)")
        print("\nRun: python collect_futures.py --test")
        print("to check if NCDEX is accessible from this environment.")
        sys.exit(1)


if __name__ == "__main__":
    main()
