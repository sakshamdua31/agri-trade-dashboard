# `data/` — Commodity Intelligence Data Layer

Every dataset lives in exactly one file. Every file answers exactly one question. When something looks wrong, the folder + filename tells you where to look.

## Layout

```
data/
├── manifest.json                       # Machine-readable index of every file (source, updated, cadence, size)
├── README.md                           # This file
│
├── mandi/                              # AGMARKNET mandi observations  (LIVE, collector-updated)
│   ├── ticks.json                      #   Raw market ticks, 30-day rolling window
│   ├── daily.json                      #   Daily rollups per commodity
│   └── history.json                    #   Long-term daily modal + arrivals (2011 → present)
│
├── msp/                                # MSP / FRP  (annual updates from CACP)
│   ├── paddy.json
│   ├── wheat.json
│   ├── maize.json
│   ├── tur.json
│   ├── gram.json
│   ├── sugarcane_frp.json              # FRP, not MSP — schema is analogous
│   └── combined.json                   # Multi-crop comparison (pre-computed)
│
├── retail/                             # DOCA-PMD retail prices
│   ├── doca_items.json                 # Time series per item (36 items)
│   └── doca_state_matrix.json          # State × item snapshots (34 states × 38 items)
│
├── cpi/                                # MoSPI CPI
│   └── mospi_monthly.json              # Per-commodity monthly index, base 2024
│
├── apy/                                # Area, Production, Yield — state-wise (DA&FW)
│   ├── statewise_rice.json
│   ├── statewise_wheat.json
│   ├── statewise_maize.json
│   ├── statewise_tur.json
│   ├── statewise_gram.json
│   └── statewise_sugarcane.json
│
├── production_share/                   # State share of production, 3-yr avg (DA&FW)
│   ├── rice.json
│   ├── wheat.json
│   ├── maize.json
│   ├── tur.json
│   ├── gram.json
│   └── sugarcane.json
│
├── trade/                              # India I/X + Global supply
│   ├── india_rice.json                 # DGCIS monthly, Jan 2014 → present
│   ├── india_wheat.json
│   ├── india_maize.json
│   ├── usda_corn.json                  # USDA PSD — 118 countries × 5 attrs × 11 MYs
│   ├── usda_rice.json
│   ├── usda_wheat.json
│   └── usda_sugar.json
│
├── global_prices/                      # Global price benchmarks
│   ├── wb_pink_sheet.json              # WB monthly nominal USD, 1960 → present
│   └── fao_indices.json                # FAO indices (nominal + real, monthly + annual)
│
└── oilseeds_mir.json                   # LEGACY bundle — soybean + mustard (unchanged for now)
```

## File conventions

Every JSON file follows a common shape:

```json
{
  "source": "Where the data came from",
  "updated": "YYYY-MM-DD",
  "unit": "... where applicable",
  "notes": [
    "Anything a consumer needs to interpret the data correctly."
  ],
  ...payload-specific keys...
}
```

- **YoY / share percentages** are stored as decimal fractions (`0.05` = 5%), not as `5` or `"5%"`.
- **Nulls** mean "not available / not yet announced". Never use a placeholder number.
- **Dates** are ISO strings (`YYYY-MM-DD` or `YYYY-MM`).
- **Currency** is INR unless the file explicitly says otherwise (WB, USDA, FAO are USD).

## Manifest

`manifest.json` lists every file with its source, last-updated date, cadence, and byte size. Use it to see the whole freshness picture at a glance, or in the dashboard to show a "data as of" line per section.

## Cadence

| Cadence            | Files                                                                 |
|--------------------|-----------------------------------------------------------------------|
| Daily (live)       | `mandi/*.json`, `retail/*.json` (growing)                             |
| Monthly            | `cpi/*`, `global_prices/*`, `trade/usda_*`, WB Pink Sheet             |
| Monthly (delayed)  | `trade/india_*` (DGCIS releases lag ~1 month)                         |
| Annual             | `msp/*`, `apy/*`, `production_share/*`                                |

The dashboard treats all of them the same: fetch → render. Cadence only matters when you re-upload.

## Migration status

- **Trade tab / non-oilseed APY / production share** — all-new; no legacy code depends on the old bundles.
- **Price tab MSP tracker** — currently reads `config.msp` from the live `agri_data.json`. Migrate to read the latest row of `msp/<crop>.json` and drop `config.msp` afterward.
- **Price tab wholesale/compare/seasonality** — currently uses dummy RNG; wire to `mandi/history.json` (data has been there all along).
- **Oilseeds** — still uses `oilseeds_mir.json`. Untouched in this refactor to avoid breaking a working tab; revisit later.
