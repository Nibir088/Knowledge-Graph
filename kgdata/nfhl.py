"""FEMA National Flood Hazard Layer (NFHL): flood hazard zone polygons, one folder per state.

Mode "states" (default): FEMA's statewide NFHL File Geodatabases.
  * The current file name (it carries a date and changes with every update) is looked
    up through the FEMA Map Service Center search service:
        https://msc.fema.gov/portal/advanceSearch?...&selstate=<FIPS>&...  -> NFHL_STATE_DATA
  * The zip is downloaded from https://hazards.fema.gov/nfhlv2/output/State/<file>.zip
  * Layer S_FLD_HAZ_AR (flood hazard zones) is written to Parquet.
  Guam, American Samoa, the Northern Mariana Islands and the US Virgin Islands have
  no statewide NFHL file; they are skipped with a message.

Mode "bbox": a single study-area box from the NFHL map service
  (https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28). Polygon
  IDs are listed first and fetched in small batches, because some polygons are huge.

Output:
  <out>/nfhl/<ST>/flood_hazard_zones.parquet   (EPSG:4269, as published)
  <out>/nfhl/<ST>/source.json                   file name, size, effective date
  <out>/nfhl/bbox/flood_hazard_zones.parquet    (bbox mode, EPSG:4326)
Key fields: FLD_ZONE (A, AE, AO, AH, VE, X ...), ZONE_SUBTY, SFHA_TF ("T" = inside the
1%-annual-chance Special Flood Hazard Area), STATIC_BFE, DEPTH, DFIRM_ID.
"""

from __future__ import annotations

import json
from pathlib import Path

from .common import (download, extract_zip, find_gdb, get, layer_to_parquet, log, rmtree, session,
                     write_gdf)
from .states import STATES, resolve

MSC_SEARCH = "https://msc.fema.gov/portal/advanceSearch"
STATE_ZIP = "https://hazards.fema.gov/nfhlv2/output/State/{file}"
LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
FIELDS = ["OBJECTID", "FLD_AR_ID", "DFIRM_ID", "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF",
          "STATIC_BFE", "DEPTH", "LEN_UNIT", "VELOCITY", "VEL_UNIT", "SOURCE_CIT"]
STATE_FIELDS = ["DFIRM_ID", "VERSION_ID", "FLD_AR_ID", "STUDY_TYP", "FLD_ZONE", "ZONE_SUBTY",
                "SFHA_TF", "STATIC_BFE", "V_DATUM", "DEPTH", "LEN_UNIT", "VELOCITY", "VEL_UNIT",
                "DUAL_ZONE", "SOURCE_CIT", "GFID"]
COUNTY_CANDIDATES = ["001", "003", "005", "010", "020", "100", "110"]


# ----------------------------------------------------------------------- states
def state_file(s, fips: str) -> dict | None:
    """Ask the Map Service Center which statewide NFHL file is current."""
    for c in COUNTY_CANDIDATES:
        cid = f"{fips}{c}C"
        try:
            r = get(s, MSC_SEARCH, retries=3, params=dict(
                affiliate="fema", query="", selstate=fips, selcounty=fips + c,
                selcommunity=cid, searchedCid=cid, method="search"))
            items = r.json().get("EFFECTIVE", {}).get("NFHL_STATE_DATA") or []
        except Exception:  # noqa: BLE001 - HTML error page or bad county guess
            continue
        if items:
            p = items[0]
            return {"file": p["product_FILE_PATH"].lstrip("/").split("/")[-1],
                    "name": p.get("product_NAME"), "size": p.get("product_FILE_SIZE"),
                    "effective_date": p.get("product_EFFECTIVE_DATE_STRING")}
    return None


def run_states(out: Path, states="all", keep_zip: bool = True) -> None:
    import pyogrio

    s = session()
    root = Path(out) / "nfhl"
    for st in resolve(states):
        fips = STATES[st]
        dest = root / st / "flood_hazard_zones.parquet"
        log(f"[NFHL] {st}")
        info = state_file(s, fips)
        if info is None:
            log("  FEMA has no statewide NFHL file for this state/territory; skipped")
            continue
        meta = root / st / "source.json"
        if dest.exists() and meta.exists() and json.loads(meta.read_text()).get("file") == info["file"]:
            log(f"  have {dest} ({info['file']})")
            continue
        zp = download(s, STATE_ZIP.format(file=info["file"]), root / "_zips" / info["file"])
        ex = root / "_gdb" / st
        rmtree(ex)
        gdb = find_gdb(extract_zip(zp, ex))
        cols = [c for c in STATE_FIELDS if c in pyogrio.read_info(gdb, layer="S_FLD_HAZ_AR")["fields"]]
        # batches: statewide layers reach 180,000+ large polygons (NC needs > 14 GB in one read)
        layer_to_parquet(gdb, "S_FLD_HAZ_AR", dest, columns=cols, extra={"state": st})
        meta.write_text(json.dumps(info, indent=2))
        rmtree(ex)
        if not keep_zip:
            zp.unlink()


# ------------------------------------------------------------------------ bbox
def _ids(s, bbox, where) -> list[int]:
    r = get(s, LAYER, params=dict(where=where, geometry=",".join(map(str, bbox)),
                                  geometryType="esriGeometryEnvelope", inSR=4326,
                                  spatialRel="esriSpatialRelIntersects",
                                  returnIdsOnly="true", f="json"))
    return sorted(r.json().get("objectIds") or [])


def _fetch(s, ids: list[int]) -> list[dict]:
    """Fetch features by ID; split the batch in half on failure."""
    try:
        r = get(s, LAYER, retries=3, params=dict(objectIds=",".join(map(str, ids)),
                                                 outFields=",".join(FIELDS), outSR=4326,
                                                 f="geojson"))
        fc = r.json()
        if "error" in fc:
            raise RuntimeError(fc["error"])
        return fc.get("features", [])
    except Exception:  # noqa: BLE001
        if len(ids) == 1:
            log(f"    giving up on OBJECTID {ids[0]}")
            return []
        mid = len(ids) // 2
        return _fetch(s, ids[:mid]) + _fetch(s, ids[mid:])


def run_bbox(out: Path, bbox: tuple[float, float, float, float], only_sfha: bool = False,
             batch: int = 10, clip: bool = True) -> None:
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import box

    s = session()
    folder = Path(out) / "nfhl" / "bbox"
    parts_dir = folder / "_batches"
    parts_dir.mkdir(parents=True, exist_ok=True)
    where = "SFHA_TF='T'" if only_sfha else "1=1"
    ids = _ids(s, bbox, where)
    log(f"[NFHL] bbox {bbox}: {len(ids)} polygons")
    area = box(*bbox)
    for k in range(0, len(ids), batch):
        path = parts_dir / f"batch_{k // batch:05d}.parquet"
        if path.exists():
            continue
        feats = _fetch(s, ids[k:k + batch])
        gdf = gpd.GeoDataFrame.from_features(feats, crs=4326) if feats else \
            gpd.GeoDataFrame(columns=FIELDS + ["geometry"], geometry="geometry", crs=4326)
        if clip and len(gdf):
            gdf = gdf.set_geometry(gdf.geometry.make_valid().intersection(area))
            gdf = gdf[~gdf.geometry.is_empty]
        gdf.to_parquet(path, index=False)
        log(f"  batch {k // batch + 1}/{-(-len(ids) // batch)}: {len(gdf)} polygons")
    parts = [p for p in (gpd.read_parquet(p) for p in sorted(parts_dir.glob("*.parquet"))) if len(p)]
    if not parts:
        log("  no flood zones in this area")
        return
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326).drop_duplicates(subset=["OBJECTID"])
    (folder / "query.json").write_text(json.dumps({"bbox": bbox, "where": where, "clip": clip}))
    write_gdf(gdf, folder / "flood_hazard_zones.parquet")


def run(out: Path, mode: str = "states", states="all", keep_zip: bool = True,
        bbox=None, only_sfha: bool = False, batch: int = 10, clip: bool = True) -> None:
    if mode == "bbox":
        run_bbox(out, tuple(bbox), only_sfha, batch, clip)
    else:
        run_states(out, states, keep_zip)
