"""
FCI stock collector  -  https://fci.gov.in/statistical-data/stock

Extracts the FULL state-wise monthly stock table for every year 2012-2026,
every page, every row. It is deliberately all-or-nothing:

  * Pagination is driven by the site's own "Next" button becoming *disabled*
    (the real end-of-data signal) - never by a page count we tried to guess,
    so it cannot stop a year early believing it is finished.
  * Each year is loaded on a fresh page and retried up to MAX_ATTEMPTS times.
    A year is only accepted once it has walked to the disabled "Next".
  * If any year cannot be completed, the script raises -> exits non-zero ->
    the GitHub Action fails and commits NOTHING. You never get partial data.

Output (repo-relative, override with env FCI_OUT_DIR):
    data/fci_stock.json   full structured document
    data/fci_stock.csv    flat table, one row per state per month
"""

import io
import os
import re
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import pandas as pd
from playwright.sync_api import sync_playwright

URL = "https://fci.gov.in/statistical-data/stock"
YEARS = [str(y) for y in range(2012, 2027)]      # 2012 .. 2026 inclusive
OUT_DIR = os.environ.get("FCI_OUT_DIR", "data")

MAX_ATTEMPTS = 4            # per-year retries (fresh reload each time)
MAX_PAGES_PER_YEAR = 400    # hard safety ceiling
NAV_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 30_000

log = logging.getLogger("fci")

# --------------------------------------------------------------- name maps
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
ZONE_HI_EN = {
    "पश्चिम अंचल": "West Zone", "दक्षिण अंचल": "South Zone",
    "उत्तर अंचल": "North Zone", "उत्तरी अंचल": "North Zone",
    "पूर्व अंचल": "East Zone", "पूर्वी अंचल": "East Zone",
    "उत्तर पूर्व अंचल": "North-East Zone", "पूर्वोत्तर अंचल": "North-East Zone",
}
EN_FIX = {"J&K": "Jammu & Kashmir", "Uttrakhand": "Uttarakhand"}
EN_TO_HI = {en: hi for hi, en in STATE_HI_EN.items()}

FIELD_GUIDE = {
    "fci": "Stock held directly by Food Corporation of India (LMT)",
    "state_agencies": "Stock held by state procurement agencies for the central pool (LMT)",
    "central_pool": "FCI + State agencies combined = total central pool stock (LMT)",
}


def state_en(v):
    s = str(v).strip()
    return STATE_HI_EN.get(s, EN_FIX.get(s, s))


def state_hi(v):
    s = str(v).strip()
    if s in STATE_HI_EN:
        return s
    return EN_TO_HI.get(state_en(s), s)


def zone_en(v):
    return ZONE_HI_EN.get(str(v).strip(), str(v).strip())


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
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def row_to_record(row):
    vals = list(row)
    if len(vals) < 13:
        return None
    if not re.match(r"^\d+$", str(vals[0]).strip()):   # skip header/total rows
        return None
    date = iso_date(vals[3])
    yr = date[:4]
    if not yr.isdigit():
        return None
    state_cell = str(vals[2]).strip()
    return {
        "date": date,
        "state": state_en(state_cell),
        "state_hindi": state_hi(state_cell),
        "region": zone_en(vals[1]),
        "year": int(yr),
        "fci": {"rice": num(vals[4]), "wheat": num(vals[5]), "total": num(vals[6])},
        "state_agencies": {"rice": num(vals[7]), "wheat": num(vals[8]), "total": num(vals[9])},
        "central_pool": {"rice": num(vals[10]), "wheat": num(vals[11]), "total": num(vals[12])},
    }


# --------------------------------------------------------------- page helpers
def stock_table(page):
    """Return the 13-column stock DataFrame currently rendered, or None."""
    try:
        tables = pd.read_html(io.StringIO(page.content()))
    except ValueError:
        return None
    cands = [t for t in tables if t.shape[1] >= 13 and len(t) > 0]
    if not cands:
        return None
    for t in cands:
        if t.shape[1] == 13:
            return t
    return cands[0]


def _digits(text):
    return [int(x) for x in re.findall(r"\d+", text or "")]


def current_page(page):
    try:
        txt = page.locator("ul.ngx-pagination li.current").first.text_content(timeout=2000)
        d = _digits(txt)
        if d:
            return d[-1]
    except Exception:
        pass
    return 1


def has_pager(page):
    try:
        return page.locator("ul.ngx-pagination li.pagination-next").count() > 0
    except Exception:
        return False


def next_disabled(page):
    try:
        cls = page.locator("ul.ngx-pagination li.pagination-next").first.get_attribute("class") or ""
        return "disabled" in cls
    except Exception:
        return True


def prev_disabled(page):
    try:
        cls = page.locator("ul.ngx-pagination li.pagination-previous").first.get_attribute("class") or ""
        return "disabled" in cls
    except Exception:
        return True


def wait_for_any_table(page, tries=100):
    for _ in range(tries):
        if stock_table(page) is not None:
            return True
        page.wait_for_timeout(400)
    return False


def wait_for_year(page, year, tries=60):
    """Loaded iff the first data row's date belongs to `year` (format-agnostic)."""
    y = str(year)
    for _ in range(tries):
        t = stock_table(page)
        if t is not None and len(t):
            if y in str(list(t.iloc[0])[3]):
                return True
        page.wait_for_timeout(400)
    return False


def toggle_english(page):
    try:
        page.get_by_role("link", name="English", exact=True).first.click(timeout=6000)
        page.wait_for_timeout(1200)
    except Exception:
        pass   # Hindi names are mapped anyway


def find_year_select(page):
    selects = page.locator("select")
    for i in range(selects.count()):
        try:
            opts = [o.strip() for o in selects.nth(i).locator("option").all_inner_texts()]
        except Exception:
            continue
        if "2026" in opts and "2012" in opts:
            return selects.nth(i)
    return None


def load_year(page, year, tries=4):
    for _ in range(tries):
        sel = find_year_select(page)
        if sel is None:
            page.wait_for_timeout(600)
            continue
        try:
            sel.select_option(label=str(year))
        except Exception:
            page.wait_for_timeout(600)
            continue
        if wait_for_year(page, year):
            return True
    return False


def reset_to_first(page, tries=60):
    for _ in range(tries):
        if not has_pager(page) or current_page(page) <= 1 or prev_disabled(page):
            return
        try:
            page.locator("ul.ngx-pagination li.pagination-previous a").first.click(timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(250)


def click_next_and_wait(page, before, attempts=4):
    """Click Next; succeed only when the current-page counter actually increments."""
    for _ in range(attempts):
        try:
            page.locator("ul.ngx-pagination li.pagination-next a").first.click(timeout=5000)
        except Exception:
            pass
        for _ in range(60):                 # up to ~18s
            page.wait_for_timeout(300)
            if current_page(page) > before:
                return True
    return False


def collect_page(page, bucket):
    t = stock_table(page)
    if t is None or not len(t):
        return 0
    added = 0
    for _, r in t.iterrows():
        rec = row_to_record(r)
        if rec:
            bucket[(rec["date"], rec["state"])] = rec
            added += 1
    return added


def walk_year(page):
    """Walk every page. Returns (records, pages_visited, complete)."""
    bucket = {}
    reset_to_first(page)
    pages = 0
    while True:
        collect_page(page, bucket)
        pages += 1
        if not has_pager(page) or next_disabled(page):
            return list(bucket.values()), pages, True          # reached true last page
        if pages >= MAX_PAGES_PER_YEAR:
            return list(bucket.values()), pages, False         # safety -> incomplete
        before = current_page(page)
        if not click_next_and_wait(page, before):
            return list(bucket.values()), pages, False         # could not advance -> incomplete


def scrape_year(page, year):
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            if not wait_for_any_table(page):
                last = "default table never rendered"
            else:
                toggle_english(page)
                if not load_year(page, year):
                    last = "year did not load in dropdown"
                else:
                    rows, pages, complete = walk_year(page)
                    if complete and rows:
                        log.info("  %s: complete  (%d pages, %d rows)", year, pages, len(rows))
                        return rows
                    last = f"incomplete (pages={pages}, rows={len(rows)})"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        log.warning("  %s: attempt %d/%d failed - %s", year, attempt, MAX_ATTEMPTS, last)
    raise RuntimeError(f"Year {year} could NOT be fully scraped after {MAX_ATTEMPTS} attempts: {last}")


# --------------------------------------------------------------- aggregation
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
    years = sorted({r["year"] for r in records})
    return {
        "source": "Food Corporation of India (fci.gov.in/statistical-data/stock)",
        "description": "Monthly state-wise stock of wheat & rice held by FCI, State agencies, and the Central Pool",
        "unit": "Lakh Metric Tonnes (LMT)",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {
            "years": years,
            "states": len(states),
            "date_range": {"from": dates[0], "to": dates[-1]} if dates else {},
            "total_records": len(records),
        },
        "states_included": states,
        "field_guide": FIELD_GUIDE,
    }


def scrape(years):
    records = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = browser.new_context().new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)
        try:
            for y in years:
                log.info("Year %s ...", y)
                records.extend(scrape_year(page, y))
        except Exception:
            _dump_debug(page)
            raise
        finally:
            browser.close()

    uniq = {(r["date"], r["state"]): r for r in records}
    records = [uniq[k] for k in sorted(uniq)]

    # hard completeness gate: every requested year must be present
    present = {r["year"] for r in records}
    missing = [int(y) for y in years if int(y) not in present]
    if missing:
        raise RuntimeError(f"Completeness check FAILED - no rows for years: {missing}")

    return {
        "metadata": build_metadata(records),
        "national_monthly_summary": build_summary(records),
        "records": records,
    }


def _dump_debug(page):
    try:
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path="debug_page.png", full_page=True)
        log.warning("Wrote debug_page.html / debug_page.png for inspection.")
    except Exception:
        pass


def write_outputs(doc):
    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "fci_stock.json")
    cpath = os.path.join(OUT_DIR, "fci_stock.csv")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    flat = [{
        "date": r["date"], "year": r["year"], "zone": r["region"],
        "state": r["state"], "state_hindi": r["state_hindi"],
        "fci_rice": r["fci"]["rice"], "fci_wheat": r["fci"]["wheat"], "fci_total": r["fci"]["total"],
        "state_rice": r["state_agencies"]["rice"], "state_wheat": r["state_agencies"]["wheat"],
        "state_total": r["state_agencies"]["total"],
        "central_rice": r["central_pool"]["rice"], "central_wheat": r["central_pool"]["wheat"],
        "central_total": r["central_pool"]["total"],
    } for r in doc["records"]]
    pd.DataFrame(flat).to_csv(cpath, index=False)
    return jpath, cpath


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    doc = scrape(YEARS)                       # raises on any incomplete year
    jpath, cpath = write_outputs(doc)

    cov = doc["metadata"]["coverage"]
    per_year = defaultdict(int)
    for r in doc["records"]:
        per_year[r["year"]] += 1
    log.info("------------------------------------------------------------")
    log.info("DONE. %d records, %d states, %s .. %s",
             cov["total_records"], cov["states"],
             cov["date_range"].get("from"), cov["date_range"].get("to"))
    log.info("Rows per year: %s", {y: per_year[y] for y in sorted(per_year)})
    log.info("Wrote %s and %s", jpath, cpath)


if __name__ == "__main__":
    main()
