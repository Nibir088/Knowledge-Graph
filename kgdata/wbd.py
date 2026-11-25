"""USGS Watershed Boundary Dataset (WBD): HUC8 / HUC10 / HUC12 polygons.

Source: one File Geodatabase per HUC2 region on the National Map S3 bucket
    https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/HU2/GDB/WBD_<HU2>_HU2_GDB.zip
Output: <out>/wbd/<HU2>/WBDHU{8,10,12}.parquet  (EPSG:4269, 2D)
"""

from __future__ import annotations

from pathlib import Path

from .common import download, extract_zip, find_gdb, log, rmtree, session, write_gdf

URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/HU2/GDB/WBD_{hu2}_HU2_GDB.zip"


def run(out: Path, huc2: list[str], levels=(8, 10, 12), keep_gdb: bool = False) -> None:
    import pyogrio

    s = session()
    for hu2 in [h.zfill(2) for h in huc2]:
        log(f"[WBD] HU2 {hu2}")
        root = Path(out) / "wbd" / hu2
        if all((root / f"WBDHU{l}.parquet").exists() for l in levels):
            log("  already done")
            continue
        zp = download(s, URL.format(hu2=hu2), Path(out) / "wbd" / "_zips" / f"WBD_{hu2}_HU2_GDB.zip")
        gdb = find_gdb(extract_zip(zp, root / "_gdb"))
        for lvl in levels:
            gdf = pyogrio.read_dataframe(gdb, layer=f"WBDHU{lvl}")
            gdf.columns = [c if c == "geometry" else c.lower() for c in gdf.columns]
            write_gdf(gdf, root / f"WBDHU{lvl}.parquet")
        if not keep_gdb:
            rmtree(root / "_gdb")
