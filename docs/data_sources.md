# Data sources: how each one is downloaded

`scripts/download_sources.py` downloads every source below, using the settings in
`configs/sources.yaml`. Each source lives in its own module under `kgdata/`.

```bash
pip install -r requirements.txt
python scripts/download_sources.py                         # all sources in the config
python scripts/download_sources.py --only nc_deq wqp       # some of them
python scripts/download_sources.py --out /data/raw         # different output folder
```

Every downloader skips files it already has, so an interrupted run can just be
started again. To refresh a source, delete its folder under `raw/`. NHDPlus HR has
its own script; see [nhdplus_hr_download.md](nhdplus_hr_download.md).

Set these in `configs/sources.yaml` before a real run. The defaults are examples:

| Setting | Default (example) | What to set |
|---|---|---|
| `wbd.huc2` | 03, 17, 18 | HUC2 regions covering your study area |
| `atlas14.regions` | orb, sw | Atlas 14 project areas covering your CAFOs |
| `stage4.start` / `end` | 2018-09-13 … 20 (Hurricane Florence) | the storm s* |
| `nfhl.bbox` | Duplin/Sampson Co., NC | study-area box (lon/lat) |
| `wqp.statecode`, years | NC, WA, CA; 2015–2024 | area and period of samples |
| `echo.states` | all | `all` for the whole US, or a list of states |

## Summary

| Source key | Dataset | Gives you | Server |
|---|---|---|---|
| `wbd` | USGS Watershed Boundary Dataset | HUC8 / HUC10 / HUC12 polygons | prd-tnm.s3.amazonaws.com |
| `atlas14` | NOAA Atlas 14 | lagoon design storm R̄_u (25-yr, 24-h depth) | hdsc.nws.noaa.gov |
| `stage4` | NOAA Stage IV (via NWPS) | observed daily rainfall for storm s* | water.noaa.gov |
| `nfhl` | FEMA National Flood Hazard Layer | floodplain polygons | hazards.fema.gov |
| `wqp` | Water Quality Portal | nitrate, ammonia, E. coli samples | www.waterqualitydata.us |
| `echo` | EPA ECHO ICIS-NPDES | permitted facilities, inspections, violations | echo.epa.gov |
| `nc_deq` | NC DEQ permitted animal facilities | CAFO IDs, type, animal counts, lagoons | www.deq.nc.gov |
| `wa_ecology` | WA Ecology water rights (GWIS) | points of diversion, places of use | fortress.wa.gov |
| `ca_calwatrs` | CA water rights (CalWATRS, replaces eWRIMS) | points of diversion, places of use | data.ca.gov, publish.data.ca.gov |

If you run this in a restricted network, all servers in the last column must be
reachable.

---

## `wbd`: Watershed Boundary Dataset

- **Source:** one File Geodatabase per HUC2 region:
  `https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/HU2/GDB/WBD_<HU2>_HU2_GDB.zip`
- **Steps:** download the zip → extract → read layers `WBDHU8`, `WBDHU10`, `WBDHU12`
  → lower-case column names → 2D geometry → Parquet. Delete the extracted GDB.
- **Output:** `raw/wbd/<HU2>/WBDHU{8,10,12}.parquet` (EPSG:4269). Key columns:
  `huc8` / `huc10` / `huc12`, `name`, `areasqkm`, `states`.

## `atlas14`: lagoon design storm R̄_u

- **Source (grids):** `https://hdsc.nws.noaa.gov/pub/hdsc/data/<region>/<region>25yr24ha.zip`,
  an ASCII grid (~800 m cells) of the partial-duration 25-year, 24-hour depth.
- **Steps:** download → extract the `.asc` → divide by 1000 (the grid is stored in
  1/1000 inch) → GeoTIFF in inches with metadata tags.
- **Check done:** the `orb` grid at 35.0 N, 78.0 W is 7329 → 7.33 in, matching the
  PFDS point value for the same place (25-yr, 24-hr: 7.33 in).
- **Optional point mode:** set `atlas14.points` to a CSV of CAFO locations (for example
  the NC DEQ facility list). For each point the PFDS service
  (`/cgi-bin/hdsc/new/fe_text_mean.csv`) returns every duration × return period.
  Results are written to `pfds_points.csv`, at one request per second; points already
  queried are skipped.
- **Region codes:** `orb` (Ohio River basin and surrounding states, including NC, VA,
  TN, KY), `sw` (California + semiarid Southwest), `se`, `mw`, `ne`, `tx`, `inw`
  (ID/MT/WY), `ak`, `hi`, `pr`.
- **Gap:** **Washington and Oregon are not covered by Atlas 14.** Their design storms
  come from NOAA Atlas 2 and are not downloaded here.
- **Output:** `raw/atlas14/<region>_25yr24h_in.tif`, `raw/atlas14/pfds_points.csv`.

## `stage4`: observed rainfall for storm s*

- **Source:** `https://water.noaa.gov/resources/downloads/precip/stageIV/YYYY/MM/DD/`
  - `nws_precip_1day_YYYYMMDD_conus.tif` (or `.nc`), with 4 bands: observed precipitation
    (in), PRISM normal (in), departure from normal (in), percent of normal.
  - optional `ncep_stage_iv_source_files_YYYYMMDD.tar` (NCEP 24-h GRIB sources).
- **Day definition:** each file is the **24 h ending 12:00 UTC** on that date.
- **Grid:** HRAP polar stereographic, ~4.76 km cells. Reproject before sampling at
  lat/lon points.
- **Gap:** NWPS publishes **daily** totals only. Hourly Stage IV is not on this server
  (it is archived elsewhere, e.g. NCAR EOL dataset 21.093).
- **Output:** `raw/stage4/YYYYMMDD/nws_precip_1day_YYYYMMDD_conus.tif`.

## `nfhl`: FEMA flood hazard zones

- **Source:** NFHL map service layer 28, "Flood Hazard Zones" (S_FLD_HAZ_AR):
  `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28`
- **Why the method is unusual:** some NFHL polygons are huge (5 polygons were 13 MB in
  testing), so a normal bounding-box query fails with HTTP 500. The code therefore:
  1. asks only for the **IDs** of polygons in the study area (fast);
  2. downloads the polygons **10 at a time by ID**, halving a batch whenever it fails;
  3. **clips** every polygon to the study-area box (`clip: false` keeps them whole).
- **Fields:** `FLD_ZONE` (A, AE, AO, AH, VE, X …), `ZONE_SUBTY`, `SFHA_TF` (`T` = inside
  the 1%-annual-chance Special Flood Hazard Area), `STATIC_BFE`, `DEPTH`, `DFIRM_ID`.
  `only_sfha: true` downloads the SFHA polygons only.
- **Tested:** the example box returned 709 polygons.
- **Output:** `raw/nfhl/flood_hazard_zones.parquet` (EPSG:4326).
- **Whole states:** for very large areas, FEMA's per-state NFHL geodatabase from the
  Map Service Center (msc.fema.gov) is faster than the map service.

## `wqp`: Water Quality Portal

- **Source:** `https://www.waterqualitydata.us/data/{Station,Result}/search`
  (WQX 2.2 CSV, zipped).
- **Query:** one area filter (`statecode` like `US:37`, `countycode` like `US:37:061`,
  8-digit `huc`, or `bbox`), plus these characteristic names:
  `Nitrate`, `Inorganic nitrogen (nitrate and nitrite)`, `Ammonia`,
  `Ammonia and ammonium`, `Escherichia coli`.
- **Steps:** one station request for the whole period, then **one results request per
  year**, so each request stays small.
- **Output:** `raw/wqp/stations.csv` and `raw/wqp/results_<year>.csv`.
  - Results: `MonitoringLocationIdentifier`, `ActivityStartDate`, `CharacteristicName`,
    `ResultMeasureValue`, `ResultMeasure/MeasureUnitCode`, `ResultSampleFractionText`.
  - Stations: `LatitudeMeasure`, `LongitudeMeasure`, `HUCEightDigitCode`.
- **Watch out:** units and fractions vary between labs (mg/L vs mg/L as N, dissolved vs
  total). Harmonise them before use.

## `echo`: EPA ECHO ICIS-NPDES

- **Source:** `https://echo.epa.gov/files/echodownloads/npdes_downloads.zip`, about
  360 MB zipped and 2.3 GB of CSVs, refreshed weekly.
- **Steps:** download the whole zip → stream each CSV in chunks into Parquet (all
  columns as text, so IDs keep leading zeros).
  - `states: all` keeps **every state and territory**.
  - `states: [NC, WA]` keeps only rows whose permit ID starts with those state codes.
    `NPDES_VIOLATION_ENFORCEMENTS` has no permit ID, so it is filtered through the
    violation IDs kept from the three violation tables.
  Tables are written chunk by chunk, so the national files never need to fit in memory.
- **Tables:** `ICIS_FACILITIES` (with `GEOCODE_LATITUDE` / `LONGITUDE`), `ICIS_PERMITS`,
  `NPDES_INSPECTIONS`, `NPDES_PS_/CS_/SE_VIOLATIONS`, `NPDES_VIOLATION_ENFORCEMENTS`,
  formal and informal enforcement actions, `NPDES_QNCR_HISTORY`, NAICS/SIC codes,
  permit components and feature coordinates, data groups.
- **Tested:** all states took about 45 s and produced 268 MB of Parquet: 1,263,148
  facilities in 67 state/territory codes, 1,942,271 inspections, 5,066,543
  violation–enforcement links. The NC/WA/CA filter kept 113,980 facilities.
- **Output:** `raw/echo/ALL/<TABLE>.parquet` (all states) or `raw/echo/<STATES>/<TABLE>.parquet`,
  e.g. `raw/echo/CA_NC_WA/ICIS_FACILITIES.parquet`.

## `nc_deq`: NC permitted animal facilities (CAFOs)

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

- **Why not eWRIMS:** the eWRIMS GIS download page is protected against automated
  clients (it returns HTTP 503 to scripts), and eWRIMS has been replaced by
  **CalWATRS**. Its eWRIMS-era datasets on data.ca.gov are marked "DISCONTINUED".
- **Source:** California Open Data Portal (CKAN) dataset `calwatrs-geospatial`. The
  download URL is read from `https://data.ca.gov/api/3/action/package_show` on every run,
  so new releases are picked up. `legacy_ewrims: true` also downloads the frozen eWRIMS
  points-of-diversion flat file.
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
