"""NOAA Atlas 14 precipitation frequency: lagoon design storm (default 25-year, 24-hour depth).

Two ways to get it:
  * grids  - one ASCII grid per Atlas 14 project area from
             https://hdsc.nws.noaa.gov/pub/hdsc/data/<region>/<region><RP>yr<DUR>a.zip
             (partial-duration series, values in 1/1000 inch, ~800 m cells).
             Converted to GeoTIFF in inches: <out>/atlas14/<region>_<RP>yr<DUR>_in.tif
  * points - PFDS point query for each lat/lon in a CSV (e.g. CAFO locations):
             https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv
             -> <out>/atlas14/pfds_points.csv with all durations x return periods.

Region codes that exist for 25yr24h: sw (CA + semiarid SW), orb (Ohio River basin and
surrounding states, incl. NC/VA/TN/KY), se, mw, ne, tx, inw (ID/MT/WY), ak, hi, pr.
Washington and Oregon are NOT covered by Atlas 14.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from .common import download, extract_zip, get, log, session

GRID_URL = "https://hdsc.nws.noaa.gov/pub/hdsc/data/{r}/{r}{rp}yr{dur}a.zip"
PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv"


def grids(out: Path, regions: list[str], return_period: int = 25, duration: str = "24h") -> None:
    import numpy as np
    import rasterio

    s = session()
    for r in regions:
        log(f"[Atlas14] grid {r} {return_period}yr {duration}")
        base = f"{r}{return_period}yr{duration}a"
        tif = Path(out) / "atlas14" / f"{r}_{return_period}yr{duration}_in.tif"
        if tif.exists():
            log("  already done")
            continue
        zp = download(s, GRID_URL.format(r=r, rp=return_period, dur=duration),
                      Path(out) / "atlas14" / "_zips" / f"{base}.zip")
        folder = extract_zip(zp, Path(out) / "atlas14" / "_asc" / r)
        asc = next(folder.glob("*.asc"))
        with rasterio.open(asc) as src:
            data = src.read(1).astype("float32")
            nodata = src.nodata
            prof = src.profile.copy()
        data = np.where(data == nodata, np.nan, data / 1000.0)  # 1/1000 in -> in
        prof.update(driver="GTiff", dtype="float32", nodata=np.nan, compress="deflate")
        if prof.get("crs") is None:
            prof["crs"] = "EPSG:4326"
        with rasterio.open(tif, "w", **prof) as dst:
            dst.write(data, 1)
            dst.update_tags(units="inches", series="partial duration",
                            return_period_years=str(return_period), duration=duration,
                            source=GRID_URL.format(r=r, rp=return_period, dur=duration))
        log(f"  wrote {tif}")


def points(out: Path, csv_path: Path, lat_col: str = "lat", lon_col: str = "lon",
           id_col: str | None = None, series: str = "pds", pause: float = 1.0) -> None:
    """Query PFDS for each point; resumable (already-queried ids are skipped)."""
    s = session()
    pts = pd.read_csv(csv_path)
    id_col = id_col or "_row"
    if id_col == "_row":
        pts["_row"] = range(len(pts))
    dest = Path(out) / "atlas14" / "pfds_points.csv"
    done = set(pd.read_csv(dest)[id_col].astype(str)) if dest.exists() else set()
    dest.parent.mkdir(parents=True, exist_ok=True)
    for _, p in pts.iterrows():
        pid = str(p[id_col])
        if pid in done:
            continue
        r = get(s, PFDS_URL, params=dict(lat=f"{p[lat_col]:.5f}", lon=f"{p[lon_col]:.5f}",
                                         data="depth", units="english", series=series))
        df = parse_pfds(r.text)
        if df is None:
            log(f"  no Atlas 14 estimate for {pid} ({p[lat_col]}, {p[lon_col]})")
            continue
        df.insert(0, id_col, pid)
        df.insert(1, "lat", p[lat_col])
        df.insert(2, "lon", p[lon_col])
        df.to_csv(dest, mode="a", header=not dest.exists(), index=False)
        time.sleep(pause)
    log(f"  wrote {dest}")


def parse_pfds(text: str) -> pd.DataFrame | None:
    """Parse the PFDS text CSV into rows of duration x ARI depths (inches)."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("by duration for ARI"))
    except StopIteration:
        return None
    aris = [a.strip() for a in lines[start].split(":,", 1)[1].split(",")]
    rows = []
    for l in lines[start + 1:]:
        if ":," not in l:
            break
        dur, vals = l.split(":,", 1)
        rows.append([dur.strip()] + [float(v) for v in vals.split(",")])
    df = pd.DataFrame(rows, columns=["duration"] + [f"ari_{a}yr_in" for a in aris])
    return df

