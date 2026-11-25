"""NOAA/NWS Stage IV observed precipitation, as distributed by the National Water Prediction Service.

Source (one folder per day):
    https://water.noaa.gov/resources/downloads/precip/stageIV/YYYY/MM/DD/
        nws_precip_1day_YYYYMMDD_conus.tif   4 bands: observed, PRISM normal,
                                             departure, percent of normal (inches / %)
        nws_precip_1day_YYYYMMDD_conus.nc    same in netCDF
        ncep_stage_iv_source_files_YYYYMMDD.tar  NCEP 24 h GRIB source files
Each "day" is the 24 h ending 12:00 UTC on that date. Grid: HRAP polar stereographic,
~4.76 km cells.

NWPS publishes daily totals only. The archive starts 2016-01-01, but the daily
GeoTIFF/netCDF files start 2017-01-01; for 2016 only the GRIB source archive exists. Hourly Stage IV is
not on this server. Output: <out>/stage4/YYYY/YYYYMMDD/<file>.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .common import download, log, session

BASE = "https://water.noaa.gov/resources/downloads/precip/stageIV/{d:%Y/%m/%d}/"


def run(out: Path, start: str, end: str = "latest", area="conus",
        fmt: str = "tif", source_tar: bool = False, fallback_tar: bool = True) -> None:
    """start/end: YYYY-MM-DD; end 'latest' = yesterday (UTC).

    area: conus, ak, pr or a list of them. Days without a daily file (all of 2016)
    get the NCEP 24 h GRIB source archive instead when fallback_tar is true.
    """
    import requests

    s = session()
    areas = [area] if isinstance(area, str) else list(area)
    d0 = date.fromisoformat(str(start))
    d1 = (datetime.now(timezone.utc).date() - timedelta(days=1)) if str(end).lower() == "latest" \
        else date.fromisoformat(str(end))
    log(f"[Stage IV] {d0} .. {d1} ({', '.join(areas)}, {fmt})")

    def fetch(d, name) -> bool:
        try:
            download(s, BASE.format(d=d) + name, Path(out) / "stage4" / f"{d:%Y}" / f"{d:%Y%m%d}" / name)
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return False
            raise

    missing, via_tar = [], 0
    d = d0
    while d <= d1:
        got = [fetch(d, f"nws_precip_1day_{d:%Y%m%d}_{a}.{fmt}") for a in areas]
        tar = f"ncep_stage_iv_source_files_{d:%Y%m%d}.tar"
        if source_tar or (fallback_tar and not all(got)):
            if fetch(d, tar):
                via_tar += not all(got)
            elif not any(got):
                missing.append(str(d))
        elif not any(got):
            missing.append(str(d))
        d += timedelta(days=1)
    if via_tar:
        log(f"  {via_tar} days had no daily file; their GRIB source archive was downloaded instead")
    if missing:
        log(f"  {len(missing)} days not on the server (skipped), e.g. {missing[:3]}")
