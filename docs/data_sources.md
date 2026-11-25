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
  nc_deq/  ia_dnr/  ca_cafo/  wa_ecology/  ca_calwatrs/   (state-only datasets)
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
| `ia_dnr` | Iowa DNR animal feeding operations | every AFO/CAFO, animal units, head counts by species | IA only | programs.iowadnr.gov, services3.arcgis.com |
| `ca_cafo` | CA Water Boards CIWQS + Cal-FF | permitted CAFOs; satellite CAFO census with footprints | CA only | data.ca.gov, publish.data.ca.gov, the storage host data.ca.gov redirects to, huggingface.co (+ its CDN) |
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
- **Period:** the archive starts **2016-01-01**, but the server has **no daily
  GeoTIFF/netCDF files for 2016, 2019 and 2020** (and a few single days, e.g.
  2026-04-08). For those days the GRIB source archive is downloaded instead
  (`fallback_tar: true`); a full run reports about 1,100 such days. `end: latest` means
  yesterday.
  - The tar holds `<area>_YYYYMMDD_24h.grb.gz`, the same 24-h observed total but no
    normal/departure bands. 2016 tars hold **CONUS only**; 2019–2020 tars hold conus,
    ak and pr. Rainfall processing therefore has to read both GeoTIFF and GRIB.
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

## State CAFO lists: which states have one

ECHO only has CAFOs that hold an NPDES permit, which is a small share in most states
(1.5% of Iowa AFOs, and few NC hog farms, which hold state non-discharge permits).
The state lists below fill that gap. Checked in November 2026:

| State | Source key | Facilities | Notes |
|---|---|---:|---|
| NC | `nc_deq` | 2,099 | swine, cattle, wet poultry; dry-litter poultry is not permitted, so not listed |
| IA | `ia_dnr` | 14,316 (12,801 active) | all sizes, confinements and open feedlots |
| CA | `ca_cafo` (CIWQS) | 1,853 active (2,755 incl. historical, with coordinates) | permitted only; dairies dominate |
| CA | `ca_cafo` (Cal-FF) | 2,121 | satellite census, ~98% of CA CAFOs as of 2017 |
| WA | none | — | Ecology's permit system (PARIS) blocks scripts; use ECHO (`echo/WA/`) for CAFO general-permit holders |

## `ia_dnr`: Iowa animal feeding operations

- **Coverage:** Iowa only.
- **Source:** the Iowa DNR AFO siting map service, a live view of the state AFO
  database: `https://programs.iowadnr.gov/geospatial/rest/services/Agriculture/AnimalFeedingOperations/MapServer`.
  Features are read through the ArcGIS REST API (object IDs first, then batches of 500
  by POST, so the server's 2,000-record limit never truncates the result).
- **Layers** (`layers:` in the config):

  | Layer | Rows (tested) | What it is |
  |---|---:|---|
  | `afo_facilities` (default) | 14,316 | layer 3: every AFO DNR knows: `progid`, `facName`, `opStatus` (Active 12,801 / Inactive 1,515), `OperatType` (Confinement 10,300, Open Feedlot 2,507, both 1,451 …), `AnimalUnit`, head counts `Swine`, `CattleDair`, `CattleBeef`, `Chickens`, `Turkeys`, `SheepLGoat`, `Horses`, `Other`, `HUC` (HUC12), flags `ConsPermit`, `NPDESPermit` (211 Y), `MMP`, `NMP`, location method/date |
  | `afo_300au` (default) | 10,690 | DNR hosted layer: active AFOs with ≥ 300 animal units and a finer split (SwineSow, SwineGest, SwineNurs, SwineGrow, DairyHol, DairyJer, BeefFin, ChickLay, TurkeyFin …). Snapshot, last edited May 2024 |
  | `ag_drainage_wells` | 388 | agricultural drainage wells (layer 1) |
  | `sinkholes` | ≈ 41,000 | mapped sinkholes (layer 0) |
  | `drainage_districts` | 6,925 | drainage district polygons (layer 11) |

- **Keys:** `progid` is unique per row (it is the `facilityID` in DNR's eMMP links).
  69 locations (same `OBJECTID`/`LOCID`) have two program records, e.g. an old inactive
  and a new active operation; both rows are kept.
- **Watch out:** 3 records have latitude and longitude swapped in the source; they get
  `coord_ok = False` and are not corrected. Dates are Esri timestamps converted to
  datetimes. The service is live, so a later run (with `refresh: true`) gives the
  current state of the database.
- **Output:** `raw/ia_dnr/<layer>.parquet` (EPSG:4326).

## `ca_cafo`: California CAFOs

- **Coverage:** California only. Two sources (`sources: [ciwqs, cal_ff]`):
- **CIWQS** (State Water Board), California Open Data dataset
  `surface-water-water-quality-regulated-facility-information`, resource "Confined
  Animal Facilities Regulated by the Water Boards". A daily export of all
  animal-waste regulatory measures (programs ANIWSTCOWS, ANIWSTOTH, ANIWSTGRZ). This is
  the same data as the CIWQS *Regulated Facility* web report with program ANIMALWASTE
  (that report's Excel export only gives totals per region: 1,826 active facilities
  on 2026-11-25).
  - One row per regulatory measure: `reg_measure_type` (WDR, general-order enrollee,
    NPDES, waiver …), `status` (Active / Historical), `facility_id`, `wdid`, `cafo_type`
    (Dairies, Feedlots, Poultry, Hogs, Other), `cafo_subtype`, `cafo_population`,
    `animal_equivalent_units_aeu`, `latitude_decimal_degrees`, `longitude_decimal_degrees`.
  - `ciwqs_cafo_facilities.parquet` keeps one point per `facility_id`, preferring its
    active measure, with `n_measures` and `any_active`.
  - **Tested:** 4,856 measures (1,889 active) → 2,755 facilities with coordinates
    (1,853 with an active measure). 4 historical dairies have a longitude without its
    minus sign → `coord_ok = False`.
- **Cal-FF** (Stanford RegLab, Hugging Face `reglab/cal-ff`, CC0; Magesh et al.,
  *Scientific Data* 2025). CAFOs found in satellite imagery by a computer-vision model and
  checked by annotators; about 98% of California CAFOs as of 2017, including facilities
  missing from permit records.
  - `cal_ff_facilities.parquet`: 2,121 facility footprints (MultiPolygon) with
    `animal_type`, `n_buildings`, `footprint_sq_m`, county, census tract,
    construction/destruction date bounds, and the matched permits
    (`best_permit_matches`, stored as JSON text).
  - `cal_ff_<table>.parquet`: `permits` (3,761 CIWQS permits used for matching),
    `animal_typing`, `construction_dating` (has `has_lagoon`, `where_animals_stay`),
    `parcel_locations`, `parcel_name_annotations`, `county_groups`.
    `cal_ff_all: true` adds `counties` (11 MB) and `images` (92 MB).
  - Addresses are redacted; RegLab shares them on request for research.
- **Output:** `raw/ca_cafo/*.parquet`, original files in `raw/ca_cafo/_raw/`.

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
