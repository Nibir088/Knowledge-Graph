# NHDPlus HR download: how it is done

This document describes exactly what `scripts/download_nhdplus_hr.py` does,
step by step, so the NHDPlus HR inputs to the knowledge graph can be
reproduced and checked.

## 1. What NHDPlus HR is and where it comes from

NHDPlus High Resolution (NHDPlus HR) is the USGS 1:24,000-scale national
hydrography network with derived attributes (flow routing, value-added
attributes, catchments, and EROM mean-annual flow estimates).

USGS distributes it as one Esri File Geodatabase per **HUC4** (a 4-digit
hydrologic unit, also called a VPU), zipped, on the National Map S3 bucket:

```
https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/NHDPLUS_H_<HUC4>_HU4_GDB.zip
```

- 233 HUC4 zips, about 50 GB in total; the largest is about 1.4 GB zipped.
- The files are dated 2023. USGS no longer updates NHDPlus HR (it is being
  replaced by the 3D Hydrography Program), so this is a frozen snapshot.
- The National Map search API (`tnmaccess.nationalmap.gov`) can also locate
  these files, but the script does not need it: it reads the bucket directly.

## 2. Pipeline overview

```
S3 bucket listing ──> choose HUC4s ──> download zip ──> extract .gdb
      ──> read 6 layers (selected fields only) ──> clean / filter
      ──> <out>/<HUC4>/<layer>.parquet ──> (optional) <out>/merged/<layer>.parquet
```

Each step is described below.

## 3. Step by step

### 3.1 List what is available

The script requests the public S3 bucket listing
(`?list-type=2&prefix=StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/`),
following `NextContinuationToken` until the listing is complete, and keeps
every key matching `NHDPLUS_H_<HUC4>_HU4_GDB.zip` together with its size.

`--list` prints this table and exits. Every requested HUC4 is checked
against it, so a typo fails before anything is downloaded.

### 3.2 Choose the HUC4s

- `--huc4 0710 0708` selects regions directly.
- `--huc2 07` selects every available HUC4 starting with `07`.
- Both can be combined; duplicates are removed.

The script prints the selected HUC4s and the total zip size before starting.

### 3.3 Download (resumable)

For each HUC4:

1. A `HEAD` request gets the exact `Content-Length`.
2. If `<out>/_zips/NHDPLUS_H_<HUC4>_HU4_GDB.zip` already exists with that size,
   it is reused.
3. Otherwise data is streamed into `<name>.zip.part`. If a `.part` file exists,
   the request sends `Range: bytes=<have>-` and appends; if the server does not
   answer `206 Partial Content`, the download restarts from zero.
4. When the `.part` file reaches the expected size it is renamed to `.zip`.
   A short file counts as a failure.
5. Failures (network errors, truncated transfers) are retried up to 8 times
   with exponential backoff (5 s, 10 s, 20 s, ... capped at 300 s).

`--download-only` stops after this step.

### 3.4 Extract the geodatabase

The zip is opened and must contain exactly one `*.gdb/` folder. Every member
path is checked to stay inside the destination directory (protection against
malicious paths), and only the `.gdb` members are extracted to
`<out>/_gdb/<HUC4>/`. A `.extracted` marker file inside the `.gdb` lets later
runs skip re-extraction.

### 3.5 Read the layers

The layers are read with `pyogrio` (GDAL's OpenFileGDB driver). Only the
fields below are read, never the whole layer:

| Layer | Fields read | Geometry | Becomes in the graph |
|---|---|---|---|
| `NHDFlowline` | NHDPlusID, Permanent_Identifier, FType, FCode, LengthKM, WBArea_Permanent_Identifier | lines | reach nodes V_R; reach–pond links via WBArea_Permanent_Identifier |
| `NHDPlusFlow` | FromNHDPID, ToNHDPID | none | `flows_to` edges |
| `NHDPlusFlowlineVAA` | NHDPlusID, Divergence, HydroSeq, StreamOrde | none | branch split share f_e; acyclicity check |
| `NHDPlusEROMMA` | NHDPlusID, QAMA, VAMA | none | discharge Q_w; travel time τ_e |
| `NHDPlusCatchment` | NHDPlusID, AreaSqKm | polygons | `drains_to` weight f = overlap area ÷ CAFO or field area |
| `NHDWaterbody` | Permanent_Identifier, NHDPlusID, FType, FCode, AreaSqKm | polygons | pond nodes V_P |

Field names are matched case-insensitively against the geodatabase's actual
schema. If a field is missing, the script prints a warning listing the fields
that do exist and continues without it.

### 3.6 Clean the data

These transformations are applied to every HUC4:

| Change | Why |
|---|---|
| Geometries forced to 2D (Z and M dropped) | flowlines are stored as measured 3D lines, which most tools do not need |
| CRS kept as the source CRS, NAD83 (EPSG:4269) | no reprojection error is introduced; reproject downstream for area/length work |
| `NHDPlusID`, `FromNHDPID`, `ToNHDPID`, `HydroSeq` cast to nullable 64-bit integers | the geodatabase stores these 14-digit IDs as doubles; integers join exactly |
| `QAMA`, `VAMA` values ≤ -9000 set to NaN | USGS uses -9998 / -9999 for "not computed" |
| `StreamOrde` value -9 set to NaN (nullable integer) | USGS uses -9 for "no stream order", e.g. on coastline reaches |
| `ftype_name` column added to flowlines and waterbodies | readable FType labels (see 3.8) |
| `huc4` column added to every table | provenance after merging |

### 3.7 Clip to a study area (optional)

- `--bbox MINX MINY MAXX MAXY` (degrees): the box is passed to GDAL, which
  returns features whose bounding box intersects it.
- `--mask file.gpkg`: the mask's bounds (in EPSG:4269) are used as the box,
  then features are kept only if they intersect the union of the mask polygons.

Features are **selected, not cut**: a flowline or catchment crossing the
boundary is kept whole, so lengths and areas stay correct.

After clipping, the attribute tables are reduced to the kept reaches:

- `NHDPlusFlowlineVAA` and `NHDPlusEROMMA`: rows whose `NHDPlusID` is a kept
  flowline.
- `NHDPlusFlow`: rows where **either** `FromNHDPID` or `ToNHDPID` is a kept
  flowline, so edges leaving or entering the study area are preserved.

### 3.8 Code tables

FType values in `ftype_name`:

| Layer | FType | Name |
|---|---|---|
| NHDFlowline | 334 | Connector |
| NHDFlowline | 336 | CanalDitch |
| NHDFlowline | 420 | UndergroundConduit |
| NHDFlowline | 428 | Pipeline |
| NHDFlowline | 460 | StreamRiver |
| NHDFlowline | 558 | ArtificialPath (centerline through a waterbody) |
| NHDFlowline | 566 | Coastline |
| NHDWaterbody | 361 | Playa |
| NHDWaterbody | 378 | IceMass |
| NHDWaterbody | 390 | LakePond |
| NHDWaterbody | 436 | Reservoir |
| NHDWaterbody | 466 | SwampMarsh |
| NHDWaterbody | 493 | Estuary |

`Divergence` in `NHDPlusFlowlineVAA`: 0 = no split, 1 = main path at a split,
2 = minor path at a split.

### 3.9 Write the outputs

```
<out>/
  _zips/NHDPLUS_H_<HUC4>_HU4_GDB.zip     downloaded archives   (--delete-zip removes)
  _gdb/<HUC4>/NHDPLUS_H_<HUC4>_HU4_GDB.gdb  extracted GDBs     (--delete-gdb removes)
  <HUC4>/NHDFlowline.parquet             GeoParquet
  <HUC4>/NHDPlusFlow.parquet             Parquet
  <HUC4>/NHDPlusFlowlineVAA.parquet      Parquet
  <HUC4>/NHDPlusEROMMA.parquet           Parquet
  <HUC4>/NHDPlusCatchment.parquet        GeoParquet
  <HUC4>/NHDWaterbody.parquet            GeoParquet
  merged/<layer>.parquet                 with --merge
```

A HUC4 whose six Parquet files already exist is skipped unless `--force` is
given, so an interrupted run can simply be restarted.

### 3.10 Merge across HUC4s (optional)

With `--merge`, each layer is concatenated over the requested HUC4s and
duplicates are dropped on these keys:

| Layer | Deduplication key |
|---|---|
| NHDPlusFlow | (FromNHDPID, ToNHDPID) |
| NHDWaterbody | Permanent_Identifier |
| all others | NHDPlusID |

Flow rows that cross a HUC4 boundary appear in both neighbouring regions;
merging joins the networks into one.

## 4. Using the outputs in the graph

- **Travel time.** Units differ: `LengthKM` is km, `VAMA` is ft/s, `QAMA` is ft³/s.

  ```
  τ_e [s] = LengthKM * 1000 / (VAMA * 0.3048)
  Q_w [m³/s] = QAMA * 0.0283168
  ```

  VAMA is NaN where EROM was not computed (for example every reach in HUC4
  0426), and some HUC4s have no EROM table rows at all (for example 2002,
  Hawaii). A fallback velocity is needed for those reaches.

- **Split share f_e.** `Divergence` only flags main (1) and minor (2) paths.
  The geodatabase also has a table `NHDPlusDivFracMP` with a `DivFrac` field
  (fraction of flow at a split), but this script does not extract it yet, and in
  HUC4 2002 (Hawaii) every value is -9998. Where no fraction is available, f_e
  must be assigned, for example proportional to downstream QAMA or as an equal split.

- **Outlets.** `ToNHDPID = 0` marks a flow with no downstream reach in the
  network (network outlet or terminal).

- **Acyclicity.** `HydroSeq` decreases downstream; every `flows_to` edge
  should go from a higher to a lower HydroSeq.

- **Areas.** Compute overlap areas for `drains_to` weights in an equal-area
  projection (for example EPSG:5070, CONUS Albers), not in EPSG:4269 degrees.

## 5. Verification run

The script was run end to end on two small HUC4s, then with a bounding box:

| Run | NHDFlowline | NHDPlusFlow | VAA | EROMMA | Catchment | Waterbody |
|---|---:|---:|---:|---:|---:|---:|
| HUC4 0426 | 6 | 11 | 6 | 6 (VAMA all NaN) | 1 | 10 |
| HUC4 2002 | 3,081 | 2,183 | 1,583 | 0 | 1,518 | 442 |
| merged 0426 + 2002 | 3,087 | 2,194 | 1,589 | 6 | 1,519 | 452 |
| HUC4 2002, `--bbox -156.5 20.75 -156.2 21.0` | 1,164 | 728 | 547 | 0 | 551 | 218 |

All requested fields were found in both geodatabases, IDs were written as
integers, and the CRS was EPSG:4269.

## 6. Reproduce

```bash
pip install -r requirements.txt
python scripts/download_nhdplus_hr.py --list
python scripts/download_nhdplus_hr.py --huc4 2002 0426 --out raw/nhdplus_hr --merge
```

Requirements: `requests`, `tqdm`, `geopandas`, `pyogrio`, `pyarrow`
(tested with Python 3.13).
