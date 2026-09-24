#!/usr/bin/env python3
"""
dgft_fetch.py — pull DGFT documents straight from the official site with their
REAL PDF links (content.dgft.gov.in), for the last N days, across all commodities.

WHY THIS EXISTS
  dgft.gov.in/CP is a JavaScript app that loads its list from DGFT's own server
  and serves every PDF from content.dgft.gov.in under an opaque per-file id.
  You cannot guess those ids. This script renders each official DGFT listing page,
  reads every row, and extracts the actual PDF href — no mirrors, DGFT only.

WHAT IT DOES
  - Visits the 4 official DGFT listing pages (Notification / Public Notice /
    Circular / Trade Notice).
  - Extracts: number, year, subject, date, and the direct content.dgft.gov.in PDF URL.
  - Keeps rows from the last DAYS_BACK days (default 60).
  - Tags each row with your tracked commodities (from keywords in the subject).
  - Writes dgft_policies.json in the exact shape your Policies tab's DATA array uses.

SETUP (one time)
  pip install playwright
  playwright install chromium

RUN
  python dgft_fetch.py                 # last 60 days, all documents
  python dgft_fetch.py --days 90       # last 90 days
  python dgft_fetch.py --only-commodity   # keep only rows that match a tracked commodity

OUTPUT
  dgft_policies.json  -> paste its contents in as the DATA array (or send it back to me
                         and I'll wire it into the tab).
"""

import argparse, json, re, sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ---- official DGFT listing pages (main site only) ----
CATEGORIES = [
    ("notification",  "DGFT Notification",  "export_policy"),
    ("public-notice", "DGFT Public Notice", "export_policy"),
    ("circular",      "DGFT Circular",      "procurement"),
    ("trade-notice",  "DGFT Trade Notice",  "procurement"),
]
BASE = "https://www.dgft.gov.in/CP/?opt={}"

# ---- your tracked commodities -> keywords to look for in the subject line ----
COMMODITY_KEYWORDS = {
    "Soybean":   ["soya", "soybean", "de-oiled", "doc ", "oilmeal", "oil meal", "oilcake", "oil cake", "edible oil", "vegetable oil"],
    "Mustard":   ["mustard", "rapeseed"],
    "Groundnut": ["groundnut"],
    "Sunflower": ["sunflower"],
    "Paddy":     ["rice", "paddy", "broken rice", "basmati", "rice bran"],
    "Wheat":     ["wheat", "atta", "maida"],
    "Maize":     ["maize", "corn"],
    "Gram":      ["gram", "chana", "chickpea", "bengal gram"],
    "Tur":       ["tur", "arhar", "pigeon pea", "pulses", "yellow peas", "urad", "moong", "masur", "lentil"],
    "Onion":     ["onion"],
    "Sugarcane": ["sugar", "sugarcane", "molasses", "ethanol", "jaggery"],
}

DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
PDF_HOST = "content.dgft.gov.in"


def match_commodities(subject: str):
    s = subject.lower()
    hits = [c for c, kws in COMMODITY_KEYWORDS.items() if any(k in s for k in kws)]
    return hits


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def scrape_category(page, opt: str) -> list[dict]:
    url = BASE.format(opt)
    print(f"  → {url}", file=sys.stderr)
    page.goto(url, wait_until="networkidle", timeout=60000)
    # give the table a moment to render, then wait for any DGFT PDF link to appear
    try:
        page.wait_for_selector(f"a[href*='{PDF_HOST}']", timeout=20000)
    except Exception:
        print("    (no PDF links detected — layout may have changed)", file=sys.stderr)

    rows = []
    # each result row is a <tr>; pull its cells + the row's PDF anchor
    for tr in page.query_selector_all("table tr"):
        anchor = tr.query_selector(f"a[href*='{PDF_HOST}']")
        if not anchor:
            continue
        href = anchor.get_attribute("href") or ""
        cells = [c.inner_text().strip() for c in tr.query_selector_all("td")]
        if not href or not cells:
            continue
        row_text = " | ".join(cells)
        m = DATE_RE.search(row_text)
        if not m:
            continue
        d, mo, y = m.groups()
        try:
            issued = datetime(int(y), int(mo), int(d))
        except ValueError:
            continue
        # number = first cell that looks like a notification number; subject = longest cell
        number = next((c for c in cells if re.search(r"\d", c) and len(c) < 40), cells[0] if cells else "")
        subject = max(cells, key=len)
        rows.append({"number": number, "subject": subject, "issued": issued, "href": href})
    print(f"    found {len(rows)} rows with PDF links", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="look-back window in days (default 60)")
    ap.add_argument("--only-commodity", action="store_true", help="keep only rows matching a tracked commodity")
    ap.add_argument("--headful", action="store_true", help="show the browser (debugging)")
    args = ap.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    out = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        page = browser.new_page()
        for opt, issuer, lever in CATEGORIES:
            try:
                for r in scrape_category(page, opt):
                    if r["issued"] < cutoff:
                        continue
                    coms = match_commodities(r["subject"])
                    if args.only_commodity and not coms:
                        continue
                    iso = r["issued"].strftime("%Y-%m-%d")
                    out.append({
                        "id": f"dgft-{opt}-{slugify(r['number'])}-{iso}",
                        "date": iso,
                        "eff": iso,
                        "type": lever,
                        "issuer": "DGFT · Min. of Commerce",
                        "ref": f"{issuer} No. {r['number']}",
                        "title": r["subject"],
                        "summary": r["subject"],
                        "commodities": coms or ["General"],
                        "impact": "med" if coms else "low",
                        "sig": "neu",
                        "sigLabel": "See notice",
                        "url": r["href"],        # <-- REAL official content.dgft.gov.in PDF
                        "pdf": True,
                        "gov": True,
                    })
            except Exception as e:
                print(f"  ! {opt} failed: {e}", file=sys.stderr)
        browser.close()

    # newest first, de-dupe by url
    seen, deduped = set(), []
    for r in sorted(out, key=lambda x: x["date"], reverse=True):
        if r["url"] in seen:
            continue
        seen.add(r["url"]); deduped.append(r)

    with open("dgft_policies.json", "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    print(f"\n✓ wrote {len(deduped)} DGFT documents (last {args.days} days) → dgft_policies.json", file=sys.stderr)
    by_com = {}
    for r in deduped:
        for c in r["commodities"]:
            by_com[c] = by_com.get(c, 0) + 1
    print("  by commodity:", ", ".join(f"{k} {v}" for k, v in sorted(by_com.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
