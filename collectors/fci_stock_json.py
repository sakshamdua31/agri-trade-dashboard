"""
FCI Stock scraper -> govt_stocks.json

Scrapes https://fci.gov.in/statistical-data/stock across every year the page
offers and every page of results, then writes a JSON file in this exact shape:

    {
      "metadata": { source, unit, coverage, states_included, field_guide, ... },
      "national_monthly_summary": [ {month, rice, wheat, total, states_reporting}, ... ],
      "records": [ {date, state, state_hindi, region, year,
                    fci:{rice,wheat,total},
                    state_agencies:{rice,wheat,total},
                    central_pool:{rice,wheat,total}}, ... ]
    }

Figures are in Lakh Metric Tonnes (LMT). Column mapping:
  FCI columns     -> fci
  State columns   -> state_agencies
  Central columns -> central_pool

SETUP (once):
    pip install playwright pandas lxml
    playwright install chromium
RUN:
    python fci_stock_json.py
"""

import io
import json
from datetime import datetime, timezone
import pandas as pd
from playwright.sync_api import sync_playwright

URL = "https://fci.gov.in/statistical-data/stock"
# All years the dropdown offers; trim if you only want recent ones.
YEARS = [str(y) for y in range(2026, 2011, -1)]
MAX_PAGES = 60           # per-year safety cap
OUT = "govt_stocks.json"

# --- Hindi -> English (spellings match the target file's states_included) ---
STATE_HI_EN = {
    "आन्ध्र प्रदेश": "Andhra Pradesh", "अरुणाचल प्रदेश": "Arunachal Pradesh",
    "असम": "Assam", "बिहार": "Bihar", "छत्तीसगढ": "Chhattisgarh",
    "दिल्ली": "Delhi", "गुजरात": "Gujarat", "हरियाणा": "Haryana",
    "हिमाचल प्रदेश": "Himachal Pradesh", "जम्मू और कश्मीर": "Jammu & Kashmir",
    "झारखण्ड": "Jharkhand", "कर्नाटक": "Karnataka", "केरल": "Kerala",
    "मध्य प्रदेश": "Madhya Pradesh", "महाराष्ट्र": "Maharashtra",
    "मणिपुर": "Manipur", "मेघालय": "Meghalaya", "मिजोरम": "Mizoram",
    "नागालैंड": "Nagaland", "ओडिशा": "Odisha", "पंजाब": "Punjab",
    "राजस्थान": "Rajasthan", "तमिलनाडु": "Tamil Nadu", "तेलंगाना": "Telangana",
    "त्रिपुरा": "Tripura", "उत्तर प्रदेश": "Uttar Pradesh",
    "उत्तराखण्ड": "Uttarakhand", "पश्चिम बंगाल": "West Bengal",
}

FIELD_GUIDE = {
    "fci": "Stock held directly by Food Corporation of India (LMT)",
    "state_agencies": "Stock held by state procurement agencies for central pool (LMT)",
    "central_pool": "FCI + State agencies combined = total central pool stock (LMT)",
}


# ------------------------- parsing helpers -------------------------

def num(v):
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "NA", "nan", "None"):
        return 0.0
    try:
        return round(float(s), 2)
    except ValueError:
        return 0.0


def iso_date(v):
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def resolve_columns(cols):
    """Map the table's headers to our roles, tolerant of spacing/case."""
    m = {}
    for c in cols:
        n = " ".join(str(c).split()).strip()
        low = n.lower()
        if n == "State":
            m["state"] = c
        elif n == "Zone":
            m["zone"] = c
        elif "date" in low:
            m["date"] = c
        elif "fci" in low and "rice" in low:
            m["fci_rice"] = c
        elif "fci" in low and "wheat" in low:
            m["fci_wheat"] = c
        elif "fci" in low and "total" in low:
            m["fci_total"] = c
        elif "state" in low and "rice" in low:
            m["sa_rice"] = c
        elif "state" in low and "wheat" in low:
            m["sa_wheat"] = c
        elif "state" in low and "total" in low:
            m["sa_total"] = c
        elif "central" in low and "rice" in low:
            m["cp_rice"] = c
        elif "central" in low and "wheat" in low:
            m["cp_wheat"] = c
        elif "central" in low and "total" in low:
            m["cp_total"] = c
    return m


def row_to_record(row, cm):
    hindi_state = str(row[cm["state"]]).strip()
    date = iso_date(row[cm["date"]])
    return {
        "date": date,
        "state": STATE_HI_EN.get(hindi_state, hindi_state),
        "state_hindi": hindi_state,
        "region": str(row[cm["zone"]]).strip(),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "fci": {
            "rice": num(row[cm["fci_rice"]]),
            "wheat": num(row[cm["fci_wheat"]]),
            "total": num(row[cm["fci_total"]]),
        },
        "state_agencies": {
            "rice": num(row[cm["sa_rice"]]),
            "wheat": num(row[cm["sa_wheat"]]),
            "total": num(row[cm["sa_total"]]),
        },
        "central_pool": {
            "rice": num(row[cm["cp_rice"]]),
            "wheat": num(row[cm["cp_wheat"]]),
            "total": num(row[cm["cp_total"]]),
        },
    }


# ------------------------- scraping -------------------------

def stock_table(page):
    try:
        tables = pd.read_html(io.StringIO(page.content()))
    except ValueError:
        return None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if any("Zone" in c for c in cols) and any("Stock" in c for c in cols):
            return t
    return None


def find_year_select(page):
    """Return the <select> whose options are the year list."""
    selects = page.locator("select")
    for i in range(selects.count()):
        opts = [o.strip() for o in selects.nth(i).locator("option").all_inner_texts()]
        if "2026" in opts and "2012" in opts:
            return selects.nth(i)
    return None


def scrape_year(page, year):
    sel = find_year_select(page)
    if sel is not None:
        try:
            sel.select_option(label=year)
        except Exception as e:
            print(f"  (year {year}: could not set dropdown: {e})")
    else:
        print("  (year dropdown not found; scraping default view)")

    records, seen = [], set()
    for _ in range(MAX_PAGES):
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(600)

        t = stock_table(page)
        if t is None or t.empty:
            break
        fp = tuple(t.iloc[0].astype(str))
        if fp in seen:
            break
        seen.add(fp)

        cm = resolve_columns(t.columns)
        required = {"state", "zone", "date", "fci_rice", "cp_total"}
        if not required.issubset(cm):
            print(f"  (unexpected columns, skipping a page: {list(t.columns)})")
        else:
            for _, row in t.iterrows():
                records.append(row_to_record(row, cm))

        nxt = page.get_by_role("link", name="Next page")
        if nxt.count() == 0:
            break
        try:
            nxt.first.click()
        except Exception:
            break
    return records


# ------------------------- assembly -------------------------

def build_summary(records):
    from collections import defaultdict
    agg = defaultdict(lambda: {"rice": 0.0, "wheat": 0.0, "total": 0.0, "states": set()})
    for r in records:
        m = r["date"][:7]
        cp = r["central_pool"]
        a = agg[m]
        a["rice"] += cp["rice"]
        a["wheat"] += cp["wheat"]
        a["total"] += cp["total"]
        a["states"].add(r["state"])
    out = []
    for m in sorted(agg):
        a = agg[m]
        out.append({
            "month": m,
            "rice_lakh_tonnes": round(a["rice"], 2),
            "wheat_lakh_tonnes": round(a["wheat"], 2),
            "total_lakh_tonnes": round(a["total"], 2),
            "states_reporting": len(a["states"]),
        })
    return out


def build_metadata(records):
    dates = sorted(r["date"] for r in records)
    states = sorted({r["state"] for r in records})
    years = sorted({r["year"] for r in records if r["year"]})
    return {
        "source": "Food Corporation of India (fci.gov.in/statistical-data/stock)",
        "description": "Monthly state-wise stock position of wheat and rice held by FCI, State agencies, and Central Pool",
        "unit": "Lakh Metric Tonnes (LMT)",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {
            "states": len(states),
            "years": years,
            "date_range": {"from": dates[0], "to": dates[-1]} if dates else {},
            "total_records": len(records),
        },
        "states_included": states,
        "field_guide": FIELD_GUIDE,
    }


def main():
    records = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        for y in YEARS:
            print(f"Year {y} ...")
            got = scrape_year(page, y)
            print(f"  {len(got)} records")
            records.extend(got)
        browser.close()

    if not records:
        print("No records collected -- check the year dropdown / Next-page selectors.")
        return

    # De-duplicate on (date, state) in case any page was read twice.
    uniq = {(r["date"], r["state"]): r for r in records}
    records = [uniq[k] for k in sorted(uniq)]

    doc = {
        "metadata": build_metadata(records),
        "national_monthly_summary": build_summary(records),
        "records": records,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"Saved {OUT}: {len(records)} records, "
          f"{len(doc['national_monthly_summary'])} months.")


if __name__ == "__main__":
    main()
