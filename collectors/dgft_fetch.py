#!/usr/bin/env python3
"""
dgft_fetch.py — pull DGFT documents from the official site with their REAL PDF
links, last N days, all commodities. No mirrors.

On dgft.gov.in, clicking "Download (Type : PDF)" triggers a direct download — the
URL is built by JavaScript on click, so it is NOT sitting in the page as a link.
So this scraper CLICKS each Download and captures the real address two ways:
  • the download event's URL, and
  • the network request the click fires (content.dgft.gov.in / the file endpoint).
It paginates with "View More" until it has covered the look-back window, and writes
dgft_debug.json so anything unexpected is visible.

SETUP:  pip install playwright  &&  playwright install --with-deps chromium
RUN:    python dgft_fetch.py --days 90
        python dgft_fetch.py --days 90 --only-commodity
        python dgft_fetch.py --headful          # watch it locally
"""

import argparse, json, re, sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CATEGORIES = [
    ("notification",  "DGFT Notification",  "export_policy"),
    ("public-notice", "DGFT Public Notice", "export_policy"),
    ("circular",      "DGFT Circular",      "procurement"),
    ("trade-notice",  "DGFT Trade Notice",  "procurement"),
]
BASE = "https://www.dgft.gov.in/CP/?opt={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_ROWS_PER_CAT = 60          # safety cap
MAX_VIEW_MORE    = 12

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

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
FILE_RE = re.compile(r"(content\.dgft\.gov\.in|dgftprod|\.pdf(\?|$)|download)", re.I)
DL_TEXT = re.compile(r"Download\s*\(Type", re.I)


def parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def read_blocks(page):
    """Return the text block (number + subject + date) for each Download on the page,
    in DOM order (same order as the Download elements)."""
    return page.evaluate(r"""() => {
      const leaves = [...document.querySelectorAll('*')].filter(e =>
        e.childElementCount === 0 && /Download\s*\(Type/i.test(e.textContent || ''));
      return leaves.map(m => {
        let n = m;
        for (let i = 0; i < 7 && n; i++) {
          const t = (n.innerText || '');
          if (/\d{1,2}\/\d{1,2}\/\d{4}/.test(t) && t.replace(/\s+/g,' ').length > 40) return t.trim();
          n = n.parentElement;
        }
        return (n && n.innerText || m.textContent || '').trim();
      });
    }""")


def paginate(page, cutoff):
    for _ in range(MAX_VIEW_MORE):
        blocks = read_blocks(page)
        dates = [d for d in (parse_date(b) for b in blocks) if d]
        if (dates and min(dates) < cutoff) or len(blocks) >= MAX_ROWS_PER_CAT:
            return
        vm = page.locator("button:has-text('View More'), a:has-text('View More')")
        if vm.count() == 0:
            vm = page.get_by_text(re.compile(r"^\s*View More\s*$", re.I))
        if vm.count() == 0:
            return
        try:
            vm.first.scroll_into_view_if_needed()
            vm.first.click()
            page.wait_for_timeout(1800)
        except Exception:
            return


def parse_block(text):
    subject = ""
    number = ""
    for line in [l.strip() for l in (text or "").splitlines() if l.strip()]:
        if len(line) > len(subject) and "download" not in line.lower():
            subject = line
        if not number and re.search(r"\d", line) and len(line) < 40 and "/" in line and "download" not in line.lower():
            number = line
    if not number:
        m = re.search(r"\b(\d{1,3}/\d{4}(?:-\d{2,4})?)\b", text or "")
        number = m.group(1) if m else ""
    return number, subject


def scrape_category(page, opt, cutoff, debug):
    url = BASE.format(opt)
    print(f"  → {url}", file=sys.stderr)
    reqlog = []
    page.on("request", lambda r: reqlog.append(r.url))
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"    goto warning: {e}", file=sys.stderr)
    try:
        page.wait_for_selector("text=/Download \\(Type/i", timeout=20000)
    except Exception:
        print("    no Download elements appeared", file=sys.stderr)

    paginate(page, cutoff)

    blocks = read_blocks(page)
    dls = page.get_by_text(DL_TEXT)
    count = min(dls.count(), MAX_ROWS_PER_CAT)
    print(f"    rows on page: {count}", file=sys.stderr)

    out = []
    for i in range(count):
        meta = blocks[i] if i < len(blocks) else ""
        dt = parse_date(meta)
        if dt and dt < cutoff:
            continue
        el = dls.nth(i)
        start = len(reqlog)
        durl = None
        try:
            with page.expect_download(timeout=12000) as di:
                el.scroll_into_view_if_needed()
                el.click()
            d = di.value
            durl = d.url
            try:
                d.delete()
            except Exception:
                pass
        except PWTimeout:
            # a click may have opened a viewer tab instead of downloading
            pages = page.context.pages
            if len(pages) > 1:
                durl = pages[-1].url
                try:
                    pages[-1].close()
                except Exception:
                    pass
        except Exception as e:
            debug.setdefault("click_errors", []).append(str(e))

        # the real file address: prefer a content.dgft.gov.in / .pdf request the click fired
        file_reqs = [u for u in reqlog[start:] if FILE_RE.search(u) and not u.startswith("blob:")]
        link = None
        for u in file_reqs:
            if "content.dgft.gov.in" in u or "dgftprod" in u or u.lower().split("?")[0].endswith(".pdf"):
                link = u
                break
        if not link and file_reqs:
            link = file_reqs[0]
        if not link and durl and durl.startswith("http"):
            link = durl

        number, subject = parse_block(meta)
        if link:
            out.append({"number": number, "subject": subject, "date": dt, "url": link})

    debug[opt] = {
        "url": url,
        "download_elements": count,
        "sample_blocks": blocks[:3],
        "sample_links": [r["url"] for r in out[:5]],
        "links_found": len(out),
    }
    print(f"    PDF links captured: {len(out)}", file=sys.stderr)
    page.remove_listener("request", lambda r: None)  # noop; new context per category anyway
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--only-commodity", action="store_true")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    all_out, debug = [], {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        for opt, issuer, lever in CATEGORIES:
            ctx = browser.new_context(user_agent=UA, accept_downloads=True)
            page = ctx.new_page()
            try:
                for r in scrape_category(page, opt, cutoff, debug):
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
                ctx.close()
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

    print(f"\n✓ {len(deduped)} DGFT documents with real links → dgft_policies.json", file=sys.stderr)
    if not deduped:
        print("  0 links — download the dgft-debug artifact and send it to me.", file=sys.stderr)


if __name__ == "__main__":
    main()
