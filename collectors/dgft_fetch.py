#!/usr/bin/env python3
"""
dgft_fetch.py — pull DGFT documents from the official site with their REAL PDF
links (content.dgft.gov.in), last N days, all commodities. No mirrors.

DGFT's "Download" builds the PDF address in JavaScript on click, so a plain link
selector finds nothing. This version tries THREE sources for the real URL:
  1. the row's click-handler / href / data attributes (rendered DOM), and
  2. the background API/JSON the page loads (captured from the network), and
  3. any content.dgft.gov.in / dgftprod path found anywhere on the page.
It also writes dgft_debug.json showing exactly what DGFT returned — send that back
if the run finds 0 links and I'll finalize the extractor from it.

SETUP:  pip install playwright  &&  playwright install --with-deps chromium
RUN:    python dgft_fetch.py --days 90
        python dgft_fetch.py --days 90 --only-commodity
        python dgft_fetch.py --headful           # watch it locally
"""

import argparse, json, re, sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

CATEGORIES = [
    ("notification",  "DGFT Notification",  "export_policy"),
    ("public-notice", "DGFT Public Notice", "export_policy"),
    ("circular",      "DGFT Circular",      "procurement"),
    ("trade-notice",  "DGFT Trade Notice",  "procurement"),
]
BASE = "https://www.dgft.gov.in/CP/?opt={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

COMMODITY_KEYWORDS = {
    "Soybean":   ["soya", "soybean", "de-oiled", "oilmeal", "oil meal", "oilcake", "oil cake", "edible oil", "vegetable oil"],
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

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
DATE_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})|(\d{4})-(\d{2})-(\d{2})")


def find_pdf_url(text: str):
    """Pull a content.dgft.gov.in PDF address out of href/onclick/JSON text."""
    if not text:
        return None
    t = str(text)
    m = re.search(r"https?://[^\s\"'<>]+?\.pdf", t, re.I)
    if m:
        return m.group(0)
    m = re.search(r"(dgftprod/" + GUID + r"/[^\s\"'<>]+?\.pdf)", t, re.I)
    if m:
        return "https://content.dgft.gov.in/Website/" + m.group(1)
    m = re.search(r"(/?Website/dgftprod/[^\s\"'<>]+?\.pdf)", t, re.I)
    if m:
        return "https://content.dgft.gov.in/" + m.group(1).lstrip("/")
    # click handler like  fn('9fcbf4f3-...','Notification 61 ...pdf')
    m = re.search(r"['\"](" + GUID + r")['\"]\s*,\s*['\"]([^'\"]+?\.pdf)['\"]", t, re.I)
    if m:
        return "https://content.dgft.gov.in/Website/dgftprod/" + m.group(1) + "/" + m.group(2)
    return None


def parse_date(text: str):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        if m.group(4):
            return datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def records_from_json(obj):
    """Recursively find objects that reference a PDF; return (obj, pdf_url)."""
    found = []
    if isinstance(obj, dict):
        url = find_pdf_url(json.dumps(obj, ensure_ascii=False))
        if url:
            found.append((obj, url))
        for v in obj.values():
            found += records_from_json(v)
    elif isinstance(obj, list):
        for v in obj:
            found += records_from_json(v)
    return found


def pick(obj: dict, name_parts, avoid=()):
    for k, v in obj.items():
        lk = k.lower()
        if any(p in lk for p in name_parts) and not any(a in lk for a in avoid):
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
    return ""


def scrape_category(pw_page, opt, debug):
    url = BASE.format(opt)
    print(f"  → {url}", file=sys.stderr)
    responses = []
    pw_page.on("response", lambda r: responses.append(r))
    try:
        pw_page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"    goto warning: {e}", file=sys.stderr)
    pw_page.wait_for_timeout(4000)

    # ---- 1) read the rendered table rows + their links/handlers ----
    rows = pw_page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('table tr').forEach(tr => {
        const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText||'').trim());
        if (!cells.length) return;
        const acts = [...tr.querySelectorAll('a,button,[onclick],[data-url],[data-href]')].map(a => ({
          href: a.getAttribute('href')||'', onclick: a.getAttribute('onclick')||'',
          durl: a.getAttribute('data-url')||'', dhref: a.getAttribute('data-href')||'',
          outer: a.outerHTML.slice(0,300)
        }));
        out.push({cells, acts});
      });
      return out;
    }""")

    # ---- 2) capture JSON bodies from the network ----
    json_bodies = []
    for r in responses:
        try:
            u = r.url
            ct = (r.headers or {}).get("content-type", "")
            if ("json" in ct or "/api" in u.lower() or "notification" in u.lower()
                    or "getdynamic" in u.lower() or u.endswith(".json")):
                json_bodies.append((u, r.text()))
        except Exception:
            pass

    # ---- extract records: DOM first, then JSON ----
    out = []
    for row in rows:
        blob = " ".join(row["cells"])
        for a in row["acts"]:
            blob += " " + a["href"] + " " + a["onclick"] + " " + a["durl"] + " " + a["dhref"]
        pdf = find_pdf_url(blob)
        if pdf:
            subject = max(row["cells"], key=len) if row["cells"] else ""
            number = next((c for c in row["cells"] if re.search(r"\d", c) and len(c) < 40), "")
            out.append({"number": number, "subject": subject, "date": parse_date(blob), "url": pdf})

    if not out:  # fall back to the API JSON
        for _, body in json_bodies:
            try:
                data = json.loads(body)
            except Exception:
                continue
            for obj, pdf in records_from_json(data):
                subject = pick(obj, ["subject", "title", "description", "desc"], avoid=["file"]) \
                          or max([str(v) for v in obj.values() if isinstance(v, str)] or [""], key=len)
                number = pick(obj, ["notif", "circular", "publicnotice", "tradenotice", "docno", "number", "ntfn"],
                              avoid=["date", "year", "file", "id"])
                dt = None
                for v in obj.values():
                    dt = dt or (parse_date(v) if isinstance(v, str) else None)
                out.append({"number": number, "subject": subject, "date": dt, "url": pdf})

    # ---- debug snapshot ----
    debug[opt] = {
        "url": url,
        "table_rows": len(rows),
        "sample_row_html": [a["outer"] for row in rows[:2] for a in row["acts"]][:4],
        "json_responses": [{"url": u, "snippet": (b or "")[:1500]} for u, b in json_bodies[:4]],
        "records_extracted": len(out),
    }
    print(f"    table rows: {len(rows)} | json bodies: {len(json_bodies)} | PDF links found: {len(out)}",
          file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--only-commodity", action="store_true")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    all_out, debug = [], {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        for opt, issuer, lever in CATEGORIES:
            page = browser.new_context(user_agent=UA).new_page()
            try:
                for r in scrape_category(page, opt, debug):
                    if r["date"] and r["date"] < cutoff:
                        continue  # only drop when we could parse a date
                    s = (r["subject"] or "").lower()
                    coms = [c for c, kws in COMMODITY_KEYWORDS.items() if any(k in s for k in kws)]
                    if args.only_commodity and not coms:
                        continue
                    iso = r["date"].strftime("%Y-%m-%d") if r["date"] else ""
                    all_out.append({
                        "id": f"dgft-{opt}-{re.sub(r'[^a-z0-9]+','-',(r['number'] or r['url']).lower())[:50]}",
                        "date": iso, "eff": iso, "type": lever,
                        "issuer": "DGFT · Min. of Commerce",
                        "ref": f"{issuer} No. {r['number']}".strip(),
                        "title": r["subject"] or "(see notice)",
                        "summary": r["subject"] or "",
                        "commodities": coms or ["General"],
                        "impact": "med" if coms else "low", "sig": "neu", "sigLabel": "See notice",
                        "url": r["url"], "pdf": True, "gov": True,
                    })
            except Exception as e:
                print(f"  ! {opt} failed: {e}", file=sys.stderr)
                debug.setdefault(opt, {})["error"] = str(e)
            finally:
                page.close()
        browser.close()

    seen, deduped = set(), []
    for r in sorted(all_out, key=lambda x: x["date"], reverse=True):
        if r["url"] in seen:
            continue
        seen.add(r["url"]); deduped.append(r)

    with open("dgft_policies.json", "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)
    with open("dgft_debug.json", "w", encoding="utf-8") as f:
        json.dump(debug, f, ensure_ascii=False, indent=2)

    print(f"\n✓ {len(deduped)} DGFT documents with real PDF links → dgft_policies.json", file=sys.stderr)
    if not deduped:
        print("  0 links found — send me dgft_debug.json (Actions artifact) and I'll finalize the extractor.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
