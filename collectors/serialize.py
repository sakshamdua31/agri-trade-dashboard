"""
Serialize the Rice CPI Excel -> data/rice_forecast.json (committed to the repo).

Runs AFTER forecast.py in the same workflow, so it reads the freshly-updated
sheet. Reuses the same Microsoft Graph access (same 4 secrets, same file).

Output shape (IR values in PERCENT, all months included, blanks as null):
{
  "updated": "2026-09-07T12:00:00Z",
  "unit_note": "IR values are percentages (4.13 = 4.13%)",
  "rows": [
    {"month":"2024-08","actual_cpi":195.7,"predicted_cpi":195.53,
     "predicted_ir":9.48,"actual_ir":9.57,"error":0.10},
    ...
  ]
}
"""

import os
import io
import json
import datetime as dt
import msal
import requests
import pandas as pd
import numpy as np

TENANT_ID     = os.environ["TENANT_ID"]
CLIENT_ID     = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
SITE_ID       = os.environ["SITE_ID"]

FILE_PATH = "Agri Data Dashboard/data-sources/Forecasting/Rice_Forecasting.xlsx"
SHEET     = "Rice Forecasting"
OUT_PATH  = "data/rice_forecast.json"
GRAPH     = "https://graph.microsoft.com/v1.0"


def get_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Login failed: {result.get('error_description', result)}")
    return result["access_token"]


def download_workbook(token):
    h = {"Authorization": f"Bearer {token}"}
    meta = requests.get(f"{GRAPH}/sites/{SITE_ID}/drive/root:/{FILE_PATH}", headers=h)
    meta.raise_for_status()
    m = meta.json()
    drive_id, item_id = m["parentReference"]["driveId"], m["id"]
    content = requests.get(f"{GRAPH}/drives/{drive_id}/items/{item_id}/content", headers=h)
    content.raise_for_status()
    return content.content


def cell(v, pct=False):
    """Return a JSON-safe number (or None). If pct=True, convert a decimal to percent."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f):
        return None
    return round(f * 100, 2) if pct else round(f, 2)


def build_json(xlsx_bytes):
    df = pd.read_excel(io.BytesIO(xlsx_bytes), sheet_name=SHEET)
    df.columns = [str(c).strip() for c in df.columns]
    date_c, cpi_c, pcpi_c, retail_c, pir_c, air_c, err_c = df.columns[:7]
    df[date_c] = pd.to_datetime(df[date_c])

    rows = []
    for _, r in df.iterrows():
        d = r[date_c]
        if pd.isna(d):
            continue
        rows.append({
            "month":         d.strftime("%Y-%m"),
            "actual_cpi":    cell(r[cpi_c]),
            "predicted_cpi": cell(r[pcpi_c]),
            "predicted_ir":  cell(r[pir_c], pct=True),   # decimal -> percent
            "actual_ir":     cell(r[air_c], pct=True),
            "error":         cell(r[err_c], pct=True),
        })

    return {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit_note": "IR values are percentages (4.13 = 4.13%)",
        "rows": rows,
    }


def run():
    token = get_token()
    data = build_json(download_workbook(token))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {OUT_PATH} with {len(data['rows'])} months.")


if __name__ == "__main__":
    run()
