"""
Rice CPI forecast -> writes results back into the SharePoint Excel.

What it does, in order:
  1. Logs in to Microsoft Graph as the registered app (no human needed).
  2. Finds Rice_Forecasting.xlsx in the SharePoint site and downloads it.
  3. Reads the 'Rice Forecasting' sheet.
  4. ARIMAX: predicts the CURRENT month's CPI from its month-end retail price.
  5. ARIMA: chains that on and forecasts the NEXT 3 months from the CPI pattern.
  6. Writes Predicted CPI (col C) and Predicted IR (col E) for those months.
  7. Backfills Actual IR (col F) and Error (col G) for any month whose
     Actual CPI was just entered but whose IR is still blank.

It writes ONLY the computed columns (C, E, F, G). It never touches the two
columns the admin fills by hand (B = Actual CPI, D = Last Day's Retail Price).
"""

import os
import io
import msal
import requests
import pandas as pd
from statsmodels.tsa.statespace.sarimax import SARIMAX

# ----------------------------------------------------------------------
# 1. CONFIG  — the 4 secret values come from the environment (GitHub Secrets).
#    The site path and file location are not secret, so they live here.
# ----------------------------------------------------------------------
TENANT_ID     = os.environ["TENANT_ID"]
CLIENT_ID     = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
SITE_ID       = os.environ["SITE_ID"]

FILE_PATH = "Agri Data Dashboard/data-sources/Forecasting/Rice_Forecasting.xlsx"
SHEET     = "Rice Forecasting"
FIRST_DATA_ROW = 2          # row 1 is the header; data starts on row 2

GRAPH = "https://graph.microsoft.com/v1.0"


# ----------------------------------------------------------------------
# 2. LOG IN  — get an access token using the app's own credentials.
# ----------------------------------------------------------------------
def get_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(f"Login failed: {result.get('error_description', result)}")
    return result["access_token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 3. FIND + DOWNLOAD the workbook, then read the sheet into a DataFrame.
# ----------------------------------------------------------------------
def get_drive_item(token):
    """Locate the file by its folder path and return its drive id + item id."""
    url = f"{GRAPH}/sites/{SITE_ID}/drive/root:/{FILE_PATH}"
    r = requests.get(url, headers=headers(token))
    r.raise_for_status()
    meta = r.json()
    return meta["parentReference"]["driveId"], meta["id"]


def read_sheet(token, drive_id, item_id):
    url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/content"
    r = requests.get(url, headers=headers(token))
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), sheet_name=SHEET)
    df.columns = [str(c).strip() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"])
    df["row"] = df.index + FIRST_DATA_ROW      # Excel row number for this month
    return df


# ----------------------------------------------------------------------
# 4. WRITE one cell back, by address (e.g. "C132").
# ----------------------------------------------------------------------
def write_cell(token, drive_id, item_id, address, value):
    url = (f"{GRAPH}/drives/{drive_id}/items/{item_id}"
           f"/workbook/worksheets('{SHEET}')/range(address='{address}')")
    h = headers(token)
    h["Content-Type"] = "application/json"
    r = requests.patch(url, headers=h, json={"values": [[value]]})
    r.raise_for_status()


# ----------------------------------------------------------------------
# 5. THE FORECAST LOGIC.
# ----------------------------------------------------------------------
def run():
    token = get_token()
    drive_id, item_id = get_drive_item(token)
    df = read_sheet(token, drive_id, item_id)

    # Rename the working columns to short, safe names (positions are fixed).
    cols = list(df.columns)
    date_c, cpi_c, pcpi_c, retail_c, pir_c, air_c, err_c = cols[0], cols[1], cols[2], cols[3], cols[4], cols[5], cols[6]

    df[cpi_c]    = pd.to_numeric(df[cpi_c], errors="coerce")
    df[retail_c] = pd.to_numeric(df[retail_c], errors="coerce")

    # A lookup of actual CPI by month, for the IR year-on-year denominators.
    actual_cpi = {d: v for d, v in zip(df[date_c], df[cpi_c]) if pd.notna(v)}

    # Months that HAVE an actual CPI = training data.
    hist = df[df[cpi_c].notna()].copy()

    # Current month = first row with a retail price but no actual CPI yet.
    cur_rows = df[df[cpi_c].isna() & df[retail_c].notna()]
    if cur_rows.empty:
        print("No current month to forecast (every month with retail already has a CPI). Nothing to do.")
        return
    cur = cur_rows.iloc[0]
    print(f"Current forecast month: {cur[date_c]:%b-%Y} (Excel row {cur['row']})")

    # --- STEP 1: ARIMAX — current-month CPI from its month-end retail price ---
    mx = SARIMAX(
        hist[cpi_c], exog=hist[retail_c],
        order=(1, 1, 1), seasonal_order=(0, 0, 0, 12),
        enforce_stationarity=False, enforce_invertibility=False,
    ).fit(disp=False)
    cur_cpi = float(mx.forecast(steps=1, exog=[[cur[retail_c]]]).iloc[0])
    print(f"  ARIMAX predicted CPI for {cur[date_c]:%b-%Y}: {cur_cpi:.2f}")

    # --- STEP 2: ARIMA — chain it on, forecast the next 3 months on CPI pattern ---
    series = pd.concat([
        hist.set_index(date_c)[cpi_c],
        pd.Series([cur_cpi], index=[cur[date_c]]),
    ])
    am = SARIMAX(
        series, order=(1, 1, 1), seasonal_order=(0, 0, 0, 12),
        enforce_stationarity=False, enforce_invertibility=False,
    ).fit(disp=False)
    future = am.forecast(steps=3)

    # All predicted CPIs: current month + next 3.
    predicted = pd.concat([pd.Series([cur_cpi], index=[cur[date_c]]), future])

    # --- STEP 3: write Predicted CPI (C) and Predicted IR (E) ---
    row_of = {d: r for d, r in zip(df[date_c], df["row"])}
    for date, pcpi in predicted.items():
        r = int(row_of[date])
        write_cell(token, drive_id, item_id, f"C{r}", round(pcpi, 2))
        base = actual_cpi.get(date - pd.DateOffset(years=1))   # last year's ACTUAL CPI
        if base and pd.notna(base):
            pir = pcpi / base - 1
            write_cell(token, drive_id, item_id, f"E{r}", round(pir, 4))
        print(f"  wrote {date:%b-%Y}: Predicted CPI={pcpi:.2f}"
              + (f", Predicted IR={pir:.4f}" if base and pd.notna(base) else ""))

    # --- STEP 4: backfill Actual IR (F) + Error (G) for a just-declared month ---
    #     (a month that now has actual CPI, has a predicted IR already, but blank actual IR)
    df[pir_c] = pd.to_numeric(df[pir_c], errors="coerce")
    df[air_c] = pd.to_numeric(df[air_c], errors="coerce")
    need = df[df[cpi_c].notna() & df[air_c].isna() & df[pir_c].notna()]
    for _, m in need.iterrows():
        base = actual_cpi.get(m[date_c] - pd.DateOffset(years=1))
        if not base or pd.isna(base):
            continue
        air = m[cpi_c] / base - 1
        err = air - m[pir_c]
        r = int(m["row"])
        write_cell(token, drive_id, item_id, f"F{r}", round(air, 4))
        write_cell(token, drive_id, item_id, f"G{r}", round(err, 4))
        print(f"  backfilled {m[date_c]:%b-%Y}: Actual IR={air:.4f}, Error={err:.4f}")

    print("Done.")


if __name__ == "__main__":
    run()
