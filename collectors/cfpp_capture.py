"""
CFPP (Central Foodgrains Procurement Portal) — definitive extractor.

Opens the portal in a headless browser and captures the JSON data the page
fetches from its own backend. Because a real browser makes the call, all the
headers / session the API requires are handled automatically -- this succeeds
where calling the API by hand fails, and it does not depend on the HTML layout.

SETUP (run once):
    pip install playwright
    playwright install chromium

RUN:
    python cfpp_capture.py

Output: a 'cfpp_output/' folder with one .json file per data response the page
made, plus manifest.txt listing which file came from which URL. Open the files,
find the one holding the procurement figures (it will be obvious), and that is
your data source.
"""

import json
from pathlib import Path
from playwright.sync_api import sync_playwright

# The views you want. Part after '#' is the SPA route:
# #/<season>/<year>/<stateCode>/<commodityCode>
URLS = [
    "https://cfpp.nic.in/#/RMS/2025-2026/1/2",
]

OUT_DIR = Path("cfpp_output")
OUT_DIR.mkdir(exist_ok=True)


def slug(url: str) -> str:
    return (url.split("#/", 1)[-1].replace("/", "_") or "root")


def capture(url: str, context) -> None:
    page = context.new_page()
    hits = []  # (response_url, parsed_or_text)

    def on_response(response):
        try:
            req = response.request
            ctype = (response.headers or {}).get("content-type", "")
            # Keep genuine data calls: XHR/fetch, or anything serving JSON.
            if req.resource_type in ("xhr", "fetch") or "json" in ctype:
                body = response.text()
                if body and body.strip():
                    try:
                        hits.append((response.url, json.loads(body)))
                    except json.JSONDecodeError:
                        hits.append((response.url, body))
        except Exception:
            pass  # redirects / empty bodies — ignore

    page.on("response", on_response)
    print(f"\n-> {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(3_000)  # let any late data calls finish
    page.close()

    if not hits:
        print("   no JSON/XHR responses captured — the data call may fire on a "
              "click/filter; interact with the page in headful mode (set "
              "headless=False) and re-run.")
        return

    base = slug(url)
    manifest = []
    for i, (resp_url, payload) in enumerate(hits, 1):
        fname = f"{base}__{i}.json"
        text = json.dumps(payload, indent=2, ensure_ascii=False) \
            if not isinstance(payload, str) else payload
        (OUT_DIR / fname).write_text(text, encoding="utf-8")
        manifest.append(f"{fname}\t{resp_url}")
        print(f"   captured {fname}  ({resp_url})")

    (OUT_DIR / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        for url in URLS:
            try:
                capture(url, context)
            except Exception as e:
                print(f"   failed: {e}")
        browser.close()


if __name__ == "__main__":
    main()
