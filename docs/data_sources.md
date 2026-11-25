# Data sources: how each one is downloaded

`scripts/download_sources.py` downloads every source below, using the settings in
`configs/sources.yaml`. Each source lives in its own module under `kgdata/`.

```bash
pip install -r requirements.txt
python scripts/download_sources.py                         # all sources in the config
python scripts/download_sources.py --only nfhl wqp         # some of them
python scripts/download_sources.py --out /data/raw         # different output folder
```

**Coverage:** every source is collected for the **whole United States** (all states,
DC and the territories where the source has data). Data that comes per state, or carries
a state, is stored **one folder per state**, so a study can simply pick the folders
it needs:

```
raw/
  nfhl/NC/flood_hazard_zones.parquet     raw/nfhl/WA/...    (one folder per state)
  wqp/NC/results_2020.csv                raw/wqp/WA/...
  echo/NC/ICIS_FACILITIES.parquet        raw/echo/WA/...
  nc_deq/  wa_ecology/  ca_calwatrs/     (state-only datasets)
  wbd/<HU2>/          atlas14/<region>_...tif     stage4/<YYYY>/<YYYYMMDD>/
  (organised by watershed, NOAA region or day; they have no natural state split)
```

To restrict a source to some states, give a list instead of `all`, e.g.
`states: [NC, WA, CA]`. Every downloader skips files it already has, so an interrupted
run can be started again. NHDPlus HR has its own script; see
[nhdplus_hr_download.md](nhdplus_hr_download.md).

## Summary

| Source key | Dataset | Gives you | Stored by | Server |
|---|---|---|---|---|
| `wbd` | USGS Watershed Boundary Dataset | HUC8 / HUC10 / HUC12 polygons | HUC2 region (01–22) | prd-tnm.s3.amazonaws.com |
| `atlas14` | NOAA Atlas 14 | lagoon design storm R̄_u (25-yr, 24-h depth) | NOAA project area | hdsc.nws.noaa.gov |
| `stage4` | NOAA Stage IV (via NWPS) | observed daily rainfall | year / day | water.noaa.gov |
| `nfhl` | FEMA National Flood Hazard Layer | floodplain polygons | **state** | msc.fema.gov, hazards.fema.gov |
| `wqp` | Water Quality Portal | nitrate, ammonia, E. coli samples | **state** | www.waterqualitydata.us |
| `echo` | EPA ECHO ICIS-NPDES | permitted facilities, inspections, violations | **state** | echo.epa.gov |
| `nc_deq` | NC DEQ permitted animal facilities | CAFO IDs, type, animal counts, lagoons | NC only | www.deq.nc.gov |
| `wa_ecology` | WA Ecology water rights (GWIS) | points of diversion, places of use | WA only | fortress.wa.gov |
| `ca_calwatrs` | CA water rights (CalWATRS, replaces eWRIMS) | points of diversion, places of use | CA only | data.ca.gov, publish.data.ca.gov |

If you run this in a restricted network, all servers in the last column must be
reachable.

### Approximate sizes for the whole US

| Source | Download |
|---|---|
| `wbd` (22 regions) | a few GB |
| `atlas14` (10 regions) | under 50 MB |
| `stage4` (2016 → today, 3 areas) | about 4.5 MB per day, roughly 18 GB |
| `nfhl` (all states) | tens of GB of zips (TX ≈ 2 GB, NC ≈ 1.5 GB); `keep_zip: false` deletes them after use |
| `wqp` (all states, 2015–2024) | several GB; about 600 requests |
| `echo` | 360 MB zip → about 270 MB of Parquet |

---

## `wbd`: Watershed Boundary Dataset

- **Source:** one File Geodatabase per HUC2 region (01–22 cover all states and territories):
  `https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/HU2/GDB/WBD_<HU2>_HU2_GDB.zip`
- **Steps:** download the zip → extract → read layers `WBDHU8`, `WBDHU10`, `WBDHU12`
  → lower-case column names → 2D geometry → Parquet. Delete the extracted GDB.
- **By state:** watersheds cross state lines, so they are stored by HUC2. Each polygon
  has a `states` column (e.g. `NC,VA`) to select the states you study.
- **Output:** `raw/wbd/<HU2>/WBDHU{8,10,12}.parquet` (EPSG:4269). Key columns:
  `huc8` / `huc10` / `huc12`, `name`, `areasqkm`, `states`.

## `atlas14`: lagoon design storm R̄_u

- **Source (grids):** `https://hdsc.nws.noaa.gov/pub/hdsc/data/<region>/<region>25yr24ha.zip`,
  an ASCII grid (~800 m cells) of the partial-duration 25-year, 24-hour depth.
- **Regions collected:** `sw` (California + semiarid Southwest), `orb` (Ohio River basin
  and surrounding states, incl. NC, VA, TN, KY), `mw` (Midwest), `se` (Southeast),
  `ne` (Northeast), `tx` (Texas), `inw` (ID/MT/WY), `ak`, `hi`, `pr`.
- **Gap:** **Washington and Oregon are not covered by Atlas 14.** Their design storms
  come from NOAA Atlas 2 and are not downloaded here.
- **Steps:** download → extract the `.asc` → divide by 1000 (the grid is stored in
  1/1000 inch) → GeoTIFF in inches with metadata tags.
- **Check done:** the `orb` grid at 35.0 N, 78.0 W is 7329 → 7.33 in, matching the
  PFDS point value for the same place (25-yr, 24-hr: 7.33 in).
- **Optional point mode:** set `atlas14.points` to a CSV of CAFO locations. For each point
  the PFDS service (`/cgi-bin/hdsc/new/fe_text_mean.csv`) returns every duration ×
  return period, written to `pfds_points.csv` (one request per second, resumable).
- **Output:** `raw/atlas14/<region>_25yr24h_in.tif`, `raw/atlas14/pfds_points.csv`.

## `stage4`: observed daily rainfall

- **Source:** `https://water.noaa.gov/resources/downloads/precip/stageIV/YYYY/MM/DD/`
  - `nws_precip_1day_YYYYMMDD_<area>.tif` (or `.nc`), areas `conus`, `ak`, `pr`, with
    4 bands: observed precipitation (in), PRISM normal (in), departure from normal
    (in), percent of normal.
  - `ncep_stage_iv_source_files_YYYYMMDD.tar`: NCEP 24-h GRIB source files.
- **Period:** the archive starts **2016-01-01**, but daily GeoTIFF/netCDF files start
  **2017-01-01**. For 2016 (and any other day without a daily file) the GRIB source
  archive is downloaded instead (`fallback_tar: true`). `end: latest` means yesterday.
- **Day definition:** each file is the **24 h ending 12:00 UTC** on that date.
- **Grid:** HRAP polar stereographic, ~4.76 km cells. Reproject before sampling at
  lat/lon points; clip to the states you study.
- **Gap:** NWPS publishes **daily** totals only. Hourly Stage IV is not on this server
  (it is archived elsewhere, e.g. NCAR EOL dataset 21.093).
- **Output:** `raw/stage4/YYYY/YYYYMMDD/nws_precip_1day_YYYYMMDD_<area>.tif`.

## `nfhl`: FEMA flood hazard zones

- **Mode `states` (default):** FEMA's statewide NFHL File Geodatabases.
  1. The current file name contains a date and changes with every FEMA update, so it is
     looked up through the FEMA Map Service Center search
     (`https://msc.fema.gov/portal/advanceSearch`, item `NFHL_STATE_DATA`).
  2. The zip is downloaded from `https://hazards.fema.gov/nfhlv2/output/State/<file>.zip`.
  3. Layer `S_FLD_HAZ_AR` (flood hazard zones) is written to Parquet with a `state` column.
     The file name, size and date go to `source.json`. When FEMA publishes a newer file,
     the next run downloads it again.
  - **Gap:** Guam, American Samoa, the Northern Mariana Islands and the US Virgin Islands
    have no statewide NFHL file and are skipped with a message. Some counties, mostly
    rural, have no digital flood maps at all.
- **Mode `bbox`:** one study-area box from the NFHL map service, layer 28
  (`https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28`). Some
  polygons are huge (5 polygons were 13 MB in testing), so the code lists the polygon IDs
  first, fetches them 10 at a time (halving a batch that fails), and clips them to the box.
- **Fields:** `FLD_ZONE` (A, AE, AO, AH, VE, X …), `ZONE_SUBTY`, `SFHA_TF` (`T` = inside
  the 1%-annual-chance Special Flood Hazard Area), `STATIC_BFE`, `DEPTH`, `DFIRM_ID`.
- **Tested:** DC statewide file → 652 polygons (X 490, AE 149, A 13); a box in eastern NC
  → 709 polygons.
- **Output:** `raw/nfhl/<ST>/flood_hazard_zones.parquet` (EPSG:4269, as published),
  `raw/nfhl/<ST>/source.json`; bbox mode: `raw/nfhl/bbox/flood_hazard_zones.parquet` (EPSG:4326).

## `wqp`: Water Quality Portal

- **Source:** `https://www.waterqualitydata.us/data/{Station,Result}/search`
  (WQX 2.2 CSV, zipped).
- **Query:** one state at a time (`statecode=US:<FIPS>`) with these characteristic names:
  `Nitrate`, `Inorganic nitrogen (nitrate and nitrite)`, `Ammonia`,
  `Ammonia and ammonium`, `Escherichia coli`.
- **Steps:** per state, one station request for the whole period, then **one results
  request per year**, so each request stays small and a failed run resumes where it stopped.
- **Period:** `start_year`–`end_year` in the config (2015–2024 by default).
- **Output:** `raw/wqp/<ST>/stations.csv` and `raw/wqp/<ST>/results_<year>.csv`.
  - Results: `MonitoringLocationIdentifier`, `ActivityStartDate`, `CharacteristicName`,
    `ResultMeasureValue`, `ResultMeasure/MeasureUnitCode`, `ResultSampleFractionText`.
  - Stations: `LatitudeMeasure`, `LongitudeMeasure`, `HUCEightDigitCode`.
- **Tested:** DC, 2020 → 73 stations, 1,289 results.
- **Watch out:** units and fractions vary between labs (mg/L vs mg/L as N, dissolved vs
  total). Harmonise them before use.

## `echo`: EPA ECHO ICIS-NPDES

- **Source:** `https://echo.epa.gov/files/echodownloads/npdes_downloads.zip`, about
  360 MB zipped and 2.3 GB of CSVs, refreshed weekly.
- **Steps:** download the whole zip → stream each CSV in chunks → write the rows of each
  state to its own Parquet file (all columns as text, so IDs keep leading zeros).
  - The state is the first two letters of the permit ID (`NCA000001` → `NC`).
  - Some permit IDs start with codes that are not states (tribal permits such as `NN`,
    numeric regional prefixes, and others); they get their own folders too.
  - `NPDES_VIOLATION_ENFORCEMENTS` has no permit ID; its rows go to the state of their
    violation, or to `_unknown`.
- **Tables:** `ICIS_FACILITIES` (with `GEOCODE_LATITUDE` / `LONGITUDE`), `ICIS_PERMITS`,
  `NPDES_INSPECTIONS`, `NPDES_PS_/CS_/SE_VIOLATIONS`, `NPDES_VIOLATION_ENFORCEMENTS`,
  formal and informal enforcement actions, `NPDES_QNCR_HISTORY`, NAICS/SIC codes,
  permit components and feature coordinates, data groups.
- **Tested:** about 65 s for the national file: 1,263,148 facilities, 1,942,271
  inspections, 5,066,543 violation–enforcement links, split into 75 folders. NC + WA + CA
  together match the earlier state-filtered run exactly.
- **Output:** `raw/echo/<ST>/<TABLE>.parquet`.

## `nc_deq`: NC permitted animal facilities (CAFOs)

- **Coverage:** North Carolina only; there is no national CAFO list in these sources.
- **Source:** the "List of Permitted Animal Facilities" Excel file on the NC DEQ
  Animal Facility Map page. Its file name contains the publication date (e.g.
  `listpermittedanimalfacilities20260423xlsx`), so the code reads the current link from
  the page on every run.
- **Steps:** download → find the header row (`Permit Number`) → snake_case column names
  → write every row → group to one point per permit:
  - animal types joined into `regulated_activity`
  - `allowable_count` summed over animal types
- **Columns:** `permit_number`, `facility_name`, `regulated_operation` (Swine, Cattle,
  Poultry …), `permit_type`, `regulated_activity`, `allowable_count`, `number_of_lagoons`,
  dates, `county_name`, `location_lat_num`, `location_long_num`.
- **Tested:** 2,344 rows → 2,099 facilities.
- **Watch out:** DEQ does not state the datum of the coordinates; EPSG:4326 is assumed.
- **Output:** `raw/nc_deq/animal_facility_rows.parquet`,
  `raw/nc_deq/animal_facilities.parquet`, plus the original `.xlsx`.

## `wa_ecology`: Washington points of diversion and places of use

- **Coverage:** Washington only.
- **Source:** Ecology GIS data downloads, `https://fortress.wa.gov/ecy/gispublic/DataDownload/`
  - `WR_WAT_WaterRights.zip`: current water-rights geodatabase (~137 MB).
  - `WR_WAT_WaterDiversionsNHD.zip`: surface-water diversions snapped to NHD (2016).
- **Layers written:**

  | Layer | Rows (tested) | What it is |
  |---|---:|---|
  | `DevicePoints` | 165,330 | points of diversion / withdrawal; `D_Point_Type_CD` (WL = well; others include HW, PM, GC, RD, MW, ID) |
  | `UnverifiedDevicePoints` | 77,009 | not yet verified points, with Latitude/Longitude and WR_Doc_ID |
  | `PlaceOfUse` | 163,838 | place-of-use polygons, keyed by `WR_DOC_ID` |
  | `WaterRightRelate` | 184,977 | links points to water-right documents |
  | `WaterRightTrackingSystem` | 279,114 | water-right documents: purpose of use, quantities, irrigated area, priority date |
  | `WaterDiversions_ECY_NHD` | 37,328 | surface diversions on NHD reaches: `ReachCode`, `Measure`, `D_Point_ID`, `WR_DOC_ID` |

- **Watch out:** WA data are in Washington State Plane South (EPSG:2927); the code
  reprojects to EPSG:4326. To keep only surface-water diversions, filter
  `D_Point_Type_CD` (check Ecology's code list) or use `WaterDiversions_ECY_NHD`.
- **Output:** `raw/wa_ecology/<layer>.parquet`.

## `ca_calwatrs`: California points of diversion and places of use

- **Coverage:** California only.
- **Why not eWRIMS:** the eWRIMS GIS download page is protected against automated
  clients (it returns HTTP 503 to scripts), and eWRIMS has been replaced by
  **CalWATRS**. Its eWRIMS-era datasets on data.ca.gov are marked "DISCONTINUED".
- **Source:** California Open Data Portal (CKAN) dataset `calwatrs-geospatial`. The
  download URL is read from `https://data.ca.gov/api/3/action/package_show` on every run.
  `legacy_ewrims: true` also downloads the frozen eWRIMS points-of-diversion flat file.
- **Content:** `GEOSPATIAL_RELATIONSHIPS.csv`, with one row per link between a water
  right/claim and a mapped feature:
  - `GR_FEATURE_TYPE`: point of diversion, rediversion, storage, place of use …
  - `POLY_LATITUDE`, `POLY_LONGITUDE`, `POLY_HUC8`–`POLY_HUC12`, `GR_WATERCOURSE_NAME`
  - `WR_APP_FACEVALUE`, `GR_PURPOSE_OF_USE` and more

  The data dictionary CSV is copied next to the output.
- **Tested:** 109,480 rows, 106,807 with coordinates.
- **Watch out:** places of use are polygons in CalWATRS, but this file has only a
  representative latitude/longitude for each feature, not the polygon outline.
- **Output:** `raw/ca_calwatrs/geospatial_relationships.parquet`,
  `raw/ca_calwatrs/features.parquet` (points, EPSG:4326).
