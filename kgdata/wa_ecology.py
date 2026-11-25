"""Washington Ecology water rights (GWIS): points of diversion / withdrawal and places of use.

Sources (Ecology GIS data downloads):
  WR_WAT_WaterRights.zip        current water-rights geodatabase (refreshed often)
      DevicePoints            points of diversion/withdrawal (D_Point_Type_CD, e.g. WL = well)
      UnverifiedDevicePoints  points not yet verified (has Latitude/Longitude, WR_Doc_ID)
      PlaceOfUse              place-of-use polygons (WR_DOC_ID)
      WaterRightRelate        point <-> water-right document links
      WaterRightTrackingSystem  water-right documents (purpose, quantities, priority date)
  WR_WAT_WaterDiversionsNHD.zip surface-water diversions snapped to NHD reaches (2016):
      WaterDiversions_ECY_NHD (ReachCode, Measure, D_Point_ID, WR_DOC_ID)

Output: <out>/wa_ecology/<layer>.parquet (spatial layers reprojected to EPSG:4326)
"""

from __future__ import annotations

from pathlib import Path

from .common import download, extract_zip, find_gdb, log, rmtree, session, write_gdf

BASE = "https://fortress.wa.gov/ecy/gispublic/DataDownload/"
FILES = {
    "WR_WAT_WaterRights": ["DevicePoints", "UnverifiedDevicePoints", "PlaceOfUse",
                           "WaterRightRelate", "WaterRightTrackingSystem"],
    "WR_WAT_WaterDiversionsNHD": ["WaterDiversions_ECY_NHD"],
}


def run(out: Path, keep_gdb: bool = False) -> None:
    import pandas as pd
    import pyogrio

    s = session()
    folder = Path(out) / "wa_ecology"
    for name, layers in FILES.items():
        log(f"[WA Ecology] {name}")
        if all((folder / f"{l}.parquet").exists() for l in layers):
            log("  already done")
            continue
        zp = download(s, BASE + name + ".zip", folder / "_zips" / f"{name}.zip")
        ex = extract_zip(zp, folder / "_gdb" / f"{name}.gdb")
        try:
            gdb = find_gdb(ex)
        except FileNotFoundError:  # zip holds the .gdb contents without the folder
            gdb = ex
        for layer in layers:
            info = pyogrio.read_info(gdb, layer=layer)
            if info["geometry_type"]:
                gdf = pyogrio.read_dataframe(gdb, layer=layer).to_crs(4326)
            else:
                gdf = pd.DataFrame(pyogrio.read_dataframe(gdb, layer=layer, read_geometry=False))
            write_gdf(gdf, folder / f"{layer}.parquet")
        if not keep_gdb:
            rmtree(folder / "_gdb" / f"{name}.gdb")
