"""
FCI stock collector -> fci_stock.json

Scrapes https://fci.gov.in/statistical-data/stock and writes a JSON file with:
  metadata                  - source, unit, coverage, state list, field guide
  national_monthly_summary  - per-month central-pool rice/wheat/total totals
  records                   - one row per state per month, split into
                              fci / state_agencies / central_pool (rice/wheat/total)

Figures are Lakh Metric Tonnes (LMT). Column mapping from the page:
  FCI columns -> fci,  State columns -> state_agencies,  Central columns -> central_pool

SETUP (once):
    pip install playwright pandas lxml
    playwright install chromium
RUN:
    python fci_stock_collector.py

YEARS defaults to the current year for a quick first run. Add older years
(e.g. "2025", "2024", ... back to "2012") to build the full history.
"""

import io
import json
from datetime import datetime, timezone
from collections import defaultdict
import pandas as pd
from playwright.sync_api import sync_playwright

URL = "https://fci.gov.in/statistical-data/stock"
YEARS = [str(y) for y in range(2026, 2011, -1)]   # all years, 2026 down to 2012
MAX_PAGES = 60
OUT = "fci_stock.json"

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

ZONE_HI_EN = {
    "पश्चिम अंचल": "West Zone", "दक्षिण अंचल": "South Zone",
    "उत्तर अंचल": "North Zone", "उत्तरी अंचल": "North Zone",
    "पूर्व अंचल": "East Zone", "पूर्वी अंचल": "East Zone",
    "उत्तर पूर्व अंचल": "North-East Zone", "पूर्वोत्तर अंचल": "North-East Zone",
}

# The site's own English spellings for a couple of states differ from the
# canonical names in the target format; normalise them.
EN_FIX = {"J&K": "Jammu & Kashmir", "Uttrakhand": "Uttarakhand"}

EN_TO_HI = {en: hi for hi, en in STATE_HI_EN.items()}


def normalize_state(v):
    s = str(v).strip()
    if s in STATE_HI_EN:            # Hindi cell -> canonical English
        return STATE_HI_EN[s]
    return EN_FIX.get(s, s)         # English variant -> canonical, else as-is


def state_hindi_for(v):
    s = str(v).strip()
    if s in STATE_HI_EN:            # cell is already Hindi
        return s
    return EN_TO_HI.get(normalize_state(s), s)   # English -> Hindi


def normalize_region(v):
    s = str(v).strip()
    return ZONE_HI_EN.get(s, s)     # Hindi zone -> English, else as-is


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


# The page renders HINDI column headers, so we parse by COLUMN POSITION
# (the order is fixed regardless of language) rather than by header names.
# Columns, left to right:
#   0 Sl.No | 1 Zone | 2 State | 3 Date
#   4-6   FCI      rice / wheat / total
#   7-9   State    rice / wheat / total
#  10-12  Central  rice / wheat / total
def row_to_record(row):
    vals = list(row)
    if len(vals) < 13:
        return None
    state_cell = str(vals[2]).strip()
    date = iso_date(vals[3])
    year = int(date[:4]) if date[:4].isdigit() else None
    if year is None:          # skip header/footer or malformed rows
        return None
    return {
        "date": date,
        "state": normalize_state(state_cell),
        "state_hindi": state_hindi_for(state_cell),
        "region": normalize_region(vals[1]),
        "year": year,
        "fci": {"rice": num(vals[4]), "wheat": num(vals[5]), "total": num(vals[6])},
        "state_agencies": {"rice": num(vals[7]), "wheat": num(vals[8]), "total": num(vals[9])},
        "central_pool": {"rice": num(vals[10]), "wheat": num(vals[11]), "total": num(vals[12])},
    }


def stock_table(page):
    try:
        tables = pd.read_html(io.StringIO(page.content()))
    except ValueError:
        return None
    # The stock grid has 13 columns; headers may be Hindi or English, so
    # identify it by shape rather than by column names.
    candidates = [t for t in tables if t.shape[1] >= 13 and len(t) > 0]
    if not candidates:
        return None
    for t in candidates:
        if t.shape[1] == 13:
            return t
    return candidates[0]


def find_year_select(page):
    selects = page.locator("select")
    for i in range(selects.count()):
        opts = [o.strip() for o in selects.nth(i).locator("option").all_inner_texts()]
        if "2026" in opts and "2012" in opts:
            return selects.nth(i)
    return None


def first_fingerprint(page):
    """(fingerprint of the first data row, the table) for the current page."""
    t = stock_table(page)
    if t is None or t.empty:
        return None, None
    return tuple(t.iloc[0].astype(str)), t


def wait_for_year(page, year, tries=25):
    """Wait until the loaded table's rows belong to the requested year."""
    for _ in range(tries):
        page.wait_for_timeout(400)
        t = stock_table(page)
        if t is not None and len(t):
            d = iso_date(list(t.iloc[0])[3])
            if d[:4] == str(year):
                return True
    return False


def scrape_year(page, year):
    sel = find_year_select(page)
    if sel is not None:
        try:
            sel.select_option(label=year)
        except Exception as e:
            print(f"  (year {year}: could not set dropdown: {e})")
    else:
        print("  (year dropdown not found; scraping default view)")

    # Make sure the table for THIS year has loaded before reading page 1.
    wait_for_year(page, year)

    records = []
    for _ in range(MAX_PAGES):
        fp, t = first_fingerprint(page)
        if t is None:
            break

        for _, row in t.iterrows():
            rec = row_to_record(row)
            if rec:
                records.append(rec)

        # The pager anchors have no href (Angular click handlers), so target
        # them by class. On the last page this <a> is absent (a <span> instead).
        nxt = page.locator("li.pagination-next > a")
        if nxt.count() == 0:
            break
        try:
            nxt.first.click(timeout=5000)
        except Exception:
            break

        # Wait until the first row actually changes (page advanced / loaded).
        advanced = False
        for _ in range(20):
            page.wait_for_timeout(300)
            nf, nt = first_fingerprint(page)
            if nt is not None and nf != fp:
                advanced = True
                break
        if not advanced:
            break

    return records


def dump_debug(page):
    print("No records -- writing debug files (screenshot, HTML, dropdowns).")
    try:
        page.screenshot(path="debug_page.png", full_page=True)
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        sels = page.locator("select")
        info = [sels.nth(i).locator("option").all_inner_texts() for i in range(sels.count())]
        with open("debug_selects.txt", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("  debug dump failed:", e)


def build_summary(records):
    agg = defaultdict(lambda: {"rice": 0.0, "wheat": 0.0, "total": 0.0, "states": set()})
    for r in records:
        a = agg[r["date"][:7]]
        cp = r["central_pool"]
        a["rice"] += cp["rice"]; a["wheat"] += cp["wheat"]; a["total"] += cp["total"]
        a["states"].add(r["state"])
    return [{
        "month": m,
        "rice_lakh_tonnes": round(agg[m]["rice"], 2),
        "wheat_lakh_tonnes": round(agg[m]["wheat"], 2),
        "total_lakh_tonnes": round(agg[m]["total"], 2),
        "states_reporting": len(agg[m]["states"]),
    } for m in sorted(agg)]


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

        # Switch the site to English so headers, zones and state names render
        # in English. (Positional parsing + the Hindi maps still work if this
        # ever fails, so it's best-effort.)
        try:
            page.get_by_role("link", name="English", exact=True).first.click(timeout=8000)
            page.wait_for_timeout(1500)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            print("Switched to English.")
        except Exception as e:
            print(f"(could not click English toggle: {e}; continuing)")

        for y in YEARS:
            print(f"Year {y} ...")
            got = scrape_year(page, y)
            print(f"  {len(got)} records")
            records.extend(got)
        if not records:
            dump_debug(page)
        browser.close()

    if not records:
        print("Nothing collected -- see debug_page.png / debug_page.html / debug_selects.txt")
        return

    uniq = {(r["date"], r["state"]): r for r in records}
    records = [uniq[k] for k in sorted(uniq)]
    doc = {
        "metadata": build_metadata(records),
        "national_monthly_summary": build_summary(records),
        "records": records,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"Saved {OUT}: {len(records)} records, {len(doc['national_monthly_summary'])} months.")


if __name__ == "__main__":
    main()
