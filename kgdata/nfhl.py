"""FEMA National Flood Hazard Layer (NFHL): flood hazard zone polygons.

Source: FEMA's public NFHL map service, layer 28 "Flood Hazard Zones" (S_FLD_HAZ_AR)
    https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28

Some NFHL polygons are enormous (a county-wide zone X polygon can be several MB),
so plain bounding-box queries time out with HTTP 500. The downloader therefore
  1. lists the object IDs intersecting the study area (cheap),
  2. fetches the polygons in small batches by ID, halving a batch that fails,
  3. clips everything to the study-area box (unless clip=False).

Output: <out>/nfhl/flood_hazard_zones.parquet (EPSG:4326)
Key fields: FLD_ZONE (A, AE, AO, AH, VE, X ...), ZONE_SUBTY, SFHA_TF ("T" = inside the
1%-annual-chance Special Flood Hazard Area), STATIC_BFE, DEPTH, DFIRM_ID.
"""

from __future__ import annotations

import json
from pathlib import Path

from .common import get, log, session, write_gdf

LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
FIELDS = ["OBJECTID", "FLD_AR_ID", "DFIRM_ID", "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF",
          "STATIC_BFE", "DEPTH", "LEN_UNIT", "VELOCITY", "VEL_UNIT", "SOURCE_CIT"]


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
    except Exception:
        if len(ids) == 1:
            log(f"    giving up on OBJECTID {ids[0]}")
            return []
        mid = len(ids) // 2
        return _fetch(s, ids[:mid]) + _fetch(s, ids[mid:])


def run(out: Path, bbox: tuple[float, float, float, float], only_sfha: bool = False,
        batch: int = 10, clip: bool = True, **_ignored) -> None:
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import box

    s = session()
    folder = Path(out) / "nfhl"
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

    parts = [gpd.read_parquet(p) for p in sorted(parts_dir.glob("*.parquet"))]
    parts = [p for p in parts if len(p)]
    if not parts:
        log("  no flood zones in this area")
        return
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
    gdf = gdf.drop_duplicates(subset=["OBJECTID"])
    (folder / "query.json").write_text(json.dumps({"bbox": bbox, "where": where, "clip": clip}))
    write_gdf(gdf, folder / "flood_hazard_zones.parquet")
