"""NOAA/NWS Stage IV observed precipitation, as distributed by the National Water Prediction Service.

Source (one folder per day):
    https://water.noaa.gov/resources/downloads/precip/stageIV/YYYY/MM/DD/
        nws_precip_1day_YYYYMMDD_conus.tif   4 bands: observed, PRISM normal,
                                             departure, percent of normal (inches / %)
        nws_precip_1day_YYYYMMDD_conus.nc    same in netCDF
        ncep_stage_iv_source_files_YYYYMMDD.tar  NCEP 24 h GRIB source files
Each "day" is the 24 h ending 12:00 UTC on that date. Grid: HRAP polar stereographic,
~4.76 km cells.

NWPS publishes daily totals only. Hourly Stage IV is not on this server.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .common import download, log, session

BASE = "https://water.noaa.gov/resources/downloads/precip/stageIV/{d:%Y/%m/%d}/"


def run(out: Path, start: str, end: str, area: str = "conus",
        fmt: str = "tif", source_tar: bool = False) -> None:
    s = session()
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    log(f"[Stage IV] {d0} .. {d1} ({area}, {fmt})")
    d = d0
    while d <= d1:
        folder = Path(out) / "stage4" / f"{d:%Y%m%d}"
        name = f"nws_precip_1day_{d:%Y%m%d}_{area}.{fmt}"
        download(s, BASE.format(d=d) + name, folder / name)
        if source_tar:
            tar = f"ncep_stage_iv_source_files_{d:%Y%m%d}.tar"
            download(s, BASE.format(d=d) + tar, folder / tar)
        d += timedelta(days=1)
