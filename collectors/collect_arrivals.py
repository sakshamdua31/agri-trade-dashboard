"""
AGMARKNET Mandi Arrivals Scraper
================================
Scrapes daily commodity arrivals (tonnes) from:
  agmarknet.gov.in/PriceAndArrivals/CommodityWiseDailyReport.aspx

This page is ASP.NET WebForms — it needs ViewState handling.
The data.gov.in API only returns prices (no arrivals), so we
scrape the portal directly.

Usage:
  python collect_arrivals.py                    # today's data
  python collect_arrivals.py --date 2026-07-28  # specific date
  python collect_arrivals.py --backfill 7       # last 7 days

Output:
  data/mandi_arrivals/{commodity}_arrivals.json  (appending history)
  data/mandi_arrivals/arrivals_latest.json       (today's snapshot)
"""

import requests
import json
import os
import re
import time
import argparse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────
BASE_URL = "https://agmarknet.gov.in/PriceAndArrivals/CommodityWiseDailyReport.aspx"

# Your 6 focus commodities + their AGMARKNET commodity codes
# These codes come from the dropdown on the CommodityWiseDailyReport page.
# If a code doesn't work, run: python collect_arrivals.py --list-commodities
COMMODITIES = {
    "Wheat":     "1",
    "Maize":     "2",
    "Paddy":     "4",      # Paddy/Rice
    "Gram":      "8",      # Chana / Bengal Gram
    "Tur":       "9",      # Arhar / Tur dal
    "Soyabean":  "35",
}

# Output paths (relative to repo root)
OUTPUT_DIR = "data/mandi_arrivals"
LATEST_FILE = os.path.join(OUTPUT_DIR, "arrivals_latest.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Origin": "https://agmarknet.gov.in",
    "Referer": BASE_URL,
}

# ── Helpers ────────────────────────────────────────────────────

def extract_asp_fields(html):
    """Extract ASP.NET hidden fields (__VIEWSTATE etc.) from page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                  "__EVENTTARGET", "__EVENTARGUMENT"]:
        tag = soup.find("input", {"name": name})
        if tag:
            fields[name] = tag.get("value", "")
    return fields


def list_commodities(session):
    """Fetch the page and list all commodity dropdown options."""
    resp = session.get(BASE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the commodity dropdown (usually ddlCommodity)
    dropdown = soup.find("select", {"id": re.compile(r"ddlCommodity", re.I)})
    if not dropdown:
        # Try alternative ID patterns
        dropdown = soup.find("select", {"name": re.compile(r"Commodity", re.I)})

    if not dropdown:
        print("ERROR: Could not find commodity dropdown on page.")
        print("The page structure may have changed.")
        print("Page title:", soup.title.string if soup.title else "N/A")
        return {}

    commodities = {}
    for opt in dropdown.find_all("option"):
        val = opt.get("value", "")
        text = opt.get_text(strip=True)
        if val and val != "0" and val != "--Select--":
            commodities[text] = val
            print(f"  {val:>5}  {text}")

    return commodities


def fetch_arrivals_for_date(session, commodity_name, commodity_code, date_str):
    """
    Fetch arrivals data for one commodity on one date.
    Returns list of dicts: [{state, district, market, variety, arrivals_tonnes,
                             min_price, max_price, modal_price}, ...]
    """
    # Step 1: GET the page to obtain ViewState
    resp = session.get(BASE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    asp_fields = extract_asp_fields(resp.text)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 2: Find the form field names (they vary across ASP.NET versions)
    # Common patterns: ddlCommodity, txtDate, btnSubmit
    commodity_field = None
    date_field = None
    submit_field = None

    for sel in soup.find_all("select"):
        sel_name = sel.get("name", "")
        if "commodity" in sel_name.lower():
            commodity_field = sel_name
            break

    for inp in soup.find_all("input"):
        inp_name = inp.get("name", "")
        if "date" in inp_name.lower() and inp.get("type") != "hidden":
            date_field = inp_name
        if "submit" in inp_name.lower() or "btn" in inp_name.lower():
            if inp.get("type") in ("submit", "button", None):
                submit_field = inp_name

    if not commodity_field:
        print(f"  WARNING: Could not find commodity dropdown field name.")
        return []

    # Format date as DD/MM/YYYY (AGMARKNET format)
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    agmarknet_date = dt.strftime("%d/%m/%Y")

    # Step 3: POST with commodity + date
    post_data = {**asp_fields}
    post_data[commodity_field] = commodity_code

    if date_field:
        post_data[date_field] = agmarknet_date

    if submit_field:
        post_data[submit_field] = "Submit"

    resp2 = session.post(BASE_URL, data=post_data, headers=HEADERS, timeout=60)
    resp2.raise_for_status()

    # Step 4: Parse the results table
    return parse_results_table(resp2.text, commodity_name, date_str)


def parse_results_table(html, commodity_name, date_str):
    """Parse the HTML results table for arrivals + prices."""
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Find the data table — AGMARKNET uses GridView which renders as <table>
    # Look for table with headers matching expected columns
    tables = soup.find_all("table")

    target_table = None
    for table in tables:
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        header_text = " ".join(headers)
        if "arrival" in header_text or "market" in header_text:
            target_table = table
            break

    if not target_table:
        # Try finding by class/id patterns
        for table in tables:
            table_id = table.get("id", "")
            if "grid" in table_id.lower() or "grd" in table_id.lower():
                target_table = table
                break

    if not target_table:
        return records

    # Get header positions
    header_row = target_table.find("tr")
    if not header_row:
        return records

    headers = []
    for cell in header_row.find_all(["th", "td"]):
        headers.append(cell.get_text(strip=True).lower())

    # Map columns by keyword matching
    col_map = {}
    for i, h in enumerate(headers):
        if "state" in h and "state" not in col_map:
            col_map["state"] = i
        elif "district" in h:
            col_map["district"] = i
        elif "market" in h:
            col_map["market"] = i
        elif "variety" in h:
            col_map["variety"] = i
        elif "arrival" in h:
            col_map["arrivals"] = i
        elif "min" in h and "price" in h:
            col_map["min_price"] = i
        elif "max" in h and "price" in h:
            col_map["max_price"] = i
        elif "modal" in h:
            col_map["modal_price"] = i

    # Parse data rows
    rows = target_table.find_all("tr")[1:]  # skip header
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        def safe_get(key, default=""):
            idx = col_map.get(key)
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(strip=True)
            return default

        def safe_float(key, default=None):
            val = safe_get(key, "")
            val = val.replace(",", "").strip()
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        arrivals = safe_float("arrivals")
        if arrivals is None and safe_get("arrivals") == "":
            continue  # skip empty rows

        record = {
            "date": date_str,
            "commodity": commodity_name,
            "state": safe_get("state"),
            "district": safe_get("district"),
            "market": safe_get("market"),
            "variety": safe_get("variety"),
            "arrivals_tonnes": arrivals,
            "min_price": safe_float("min_price"),
            "max_price": safe_float("max_price"),
            "modal_price": safe_float("modal_price"),
        }
        records.append(record)

    return records


def aggregate_state_level(records):
    """Aggregate market-level records to state-level daily totals."""
    state_agg = {}
    for r in records:
        key = (r["date"], r["commodity"], r["state"])
        if key not in state_agg:
            state_agg[key] = {
                "date": r["date"],
                "commodity": r["commodity"],
                "state": r["state"],
                "total_arrivals_tonnes": 0,
                "num_markets_reporting": 0,
                "avg_modal_price": 0,
                "modal_prices": [],
            }
        agg = state_agg[key]
        if r["arrivals_tonnes"] is not None:
            agg["total_arrivals_tonnes"] += r["arrivals_tonnes"]
        agg["num_markets_reporting"] += 1
        if r["modal_price"] is not None:
            agg["modal_prices"].append(r["modal_price"])

    # Compute weighted average modal price
    results = []
    for agg in state_agg.values():
        if agg["modal_prices"]:
            agg["avg_modal_price"] = round(
                sum(agg["modal_prices"]) / len(agg["modal_prices"]), 2
            )
        del agg["modal_prices"]
        agg["total_arrivals_tonnes"] = round(agg["total_arrivals_tonnes"], 2)
        results.append(agg)

    return sorted(results, key=lambda x: (x["date"], x["commodity"], x["state"]))


def aggregate_national(records):
    """Aggregate to national daily total per commodity."""
    nat_agg = {}
    for r in records:
        key = (r["date"], r["commodity"])
        if key not in nat_agg:
            nat_agg[key] = {
                "date": r["date"],
                "commodity": r["commodity"],
                "total_arrivals_tonnes": 0,
                "num_markets_reporting": 0,
                "num_states_reporting": set(),
            }
        agg = nat_agg[key]
        if r["arrivals_tonnes"] is not None:
            agg["total_arrivals_tonnes"] += r["arrivals_tonnes"]
        agg["num_markets_reporting"] += 1
        if r["state"]:
            agg["num_states_reporting"].add(r["state"])

    results = []
    for agg in nat_agg.values():
        agg["num_states_reporting"] = len(agg["num_states_reporting"])
        agg["total_arrivals_tonnes"] = round(agg["total_arrivals_tonnes"], 2)
        results.append(agg)

    return sorted(results, key=lambda x: (x["date"], x["commodity"]))


def save_results(all_market_records, all_state_records, all_national_records, date_str):
    """Save results to JSON files."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Save latest snapshot (all commodities, today)
    latest = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": date_str,
        "national_summary": all_national_records,
        "state_detail": all_state_records,
    }
    with open(LATEST_FILE, "w") as f:
        json.dump(latest, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved latest snapshot → {LATEST_FILE}")

    # 2. Append to per-commodity history files
    for commodity in COMMODITIES:
        history_file = os.path.join(OUTPUT_DIR, f"{commodity.lower()}_arrivals.json")

        # Load existing history
        history = []
        if os.path.exists(history_file):
            with open(history_file) as f:
                history = json.load(f)

        # Filter new records for this commodity
        new_records = [r for r in all_state_records if r["commodity"] == commodity]
        if not new_records:
            continue

        # Remove existing entries for this date (to allow re-runs)
        existing_dates = {r["date"] for r in new_records}
        history = [r for r in history if r["date"] not in existing_dates]

        # Append and sort
        history.extend(new_records)
        history.sort(key=lambda x: (x["date"], x["state"]))

        with open(history_file, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        print(f"  Saved {len(new_records)} records → {history_file}")

    # 3. Save raw market-level data (for debugging / deeper analysis)
    raw_file = os.path.join(OUTPUT_DIR, f"raw_market_{date_str}.json")
    with open(raw_file, "w") as f:
        json.dump(all_market_records, f, indent=2, ensure_ascii=False)
    print(f"  Saved raw market data → {raw_file}")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape AGMARKNET mandi arrivals")
    parser.add_argument("--date", type=str, default=None,
                        help="Date to fetch (YYYY-MM-DD). Default: today.")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Backfill N days from today.")
    parser.add_argument("--list-commodities", action="store_true",
                        help="List all commodity codes from the dropdown.")
    args = parser.parse_args()

    session = requests.Session()

    # List commodities mode
    if args.list_commodities:
        print("Fetching commodity list from AGMARKNET...\n")
        list_commodities(session)
        return

    # Determine dates to fetch
    dates = []
    if args.backfill > 0:
        for i in range(args.backfill):
            d = datetime.now() - timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))
        dates.reverse()
    else:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        dates = [target]

    print(f"AGMARKNET Arrivals Scraper")
    print(f"{'=' * 40}")
    print(f"Commodities: {', '.join(COMMODITIES.keys())}")
    print(f"Dates: {dates[0]}" + (f" to {dates[-1]}" if len(dates) > 1 else ""))
    print()

    for date_str in dates:
        print(f"\n── {date_str} ──")

        all_market = []
        all_state = []
        all_national = []

        for commodity, code in COMMODITIES.items():
            print(f"  Fetching {commodity} (code={code})...", end=" ", flush=True)

            try:
                records = fetch_arrivals_for_date(session, commodity, code, date_str)
                print(f"{len(records)} markets found")

                if records:
                    all_market.extend(records)
                    state_agg = aggregate_state_level(records)
                    all_state.extend(state_agg)
                    national_agg = aggregate_national(records)
                    all_national.extend(national_agg)
                else:
                    print(f"    (no data — market holiday or not yet uploaded)")

            except Exception as e:
                print(f"ERROR: {e}")

            # Polite delay between requests
            time.sleep(2)

        # Save results for this date
        if all_market:
            save_results(all_market, all_state, all_national, date_str)
        else:
            print(f"\n  No data for {date_str} — skipping save.")

        # Delay between dates
        if len(dates) > 1:
            time.sleep(3)

    # Summary
    print(f"\n{'=' * 40}")
    print(f"Done. Output in: {OUTPUT_DIR}/")
    print(f"  arrivals_latest.json     → today's national + state summary")
    print(f"  {{commodity}}_arrivals.json → appending state-level history")
    print(f"  raw_market_{{date}}.json   → full market-level detail")


if __name__ == "__main__":
    main()
