"""California water rights: points of diversion and places of use.

eWRIMS has been replaced by CalWATRS, and the eWRIMS GIS download page blocks
automated clients. The State Water Board publishes CalWATRS on the California
Open Data Portal (CKAN):
  calwatrs-geospatial   GEOSPATIAL_RELATIONSHIPS.csv: one row per link between a
                        water right/claim and a mapped feature (point of diversion,
                        rediversion, storage, place of use, ...), with
                        POLY_LATITUDE / POLY_LONGITUDE, HUC8-12, watercourse name.
Optionally also the last eWRIMS points-of-diversion flat file (discontinued, frozen):
  california-water-rights-points-of-diversion

Download URLs are read from the CKAN API each run, so updated files are picked up.

Output:
  <out>/ca_calwatrs/geospatial_relationships.parquet   all rows
  <out>/ca_calwatrs/features.parquet                    points (EPSG:4326 from the lat/lon columns)
  <out>/ca_calwatrs/<data dictionary>.csv
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from .common import download, get, log, session, write_gdf

CKAN = "https://data.ca.gov/api/3/action/package_show"
PACKAGES = {"calwatrs-geospatial": True,
            "california-water-rights-points-of-diversion": False}  # name -> default on


def _resources(s, package: str) -> list[dict]:
    return get(s, CKAN, params={"id": package}).json()["result"]["resources"]


def run(out: Path, legacy_ewrims: bool = False) -> None:
    import geopandas as gpd

    s = session()
    folder = Path(out) / "ca_calwatrs"
    for pkg, default in PACKAGES.items():
        if not default and not legacy_ewrims:
            continue
        log(f"[CA] {pkg}")
        for res in _resources(s, pkg):
            url = res["url"]
            fname = url.rsplit("/", 1)[-1]
            download(s, url, folder / "_raw" / pkg / fname)

    raw = folder / "_raw" / "calwatrs-geospatial"
    zp = next(raw.glob("*.zip"))
    with zipfile.ZipFile(zp) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(member) as fh:
            df = pd.read_csv(fh, dtype=str, low_memory=False)
    for c in ("POLY_LATITUDE", "POLY_LONGITUDE", "POLY_NORTHING", "POLY_EASTING", "WR_APP_FACEVALUE"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.to_parquet(folder / "geospatial_relationships.parquet", index=False)
    log(f"  wrote {folder / 'geospatial_relationships.parquet'} ({len(df):,} rows)")

    pts = df.dropna(subset=["POLY_LATITUDE", "POLY_LONGITUDE"])
    gdf = gpd.GeoDataFrame(pts, geometry=gpd.points_from_xy(pts["POLY_LONGITUDE"],
                                                            pts["POLY_LATITUDE"]), crs=4326)
    write_gdf(gdf, folder / "features.parquet")
    for dd in raw.glob("*.csv"):
        (folder / dd.name).write_bytes(dd.read_bytes())
