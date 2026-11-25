"""North Carolina DEQ: list of permitted animal facilities (CAFO IDs, type, animal counts, lagoons).

Source: the "List of Permitted Animal Facilities" Excel file linked from
    https://www.deq.nc.gov/about/divisions/water-resources/water-quality-permitting/animal-feeding-operations/animal-facility-map
The file name carries its publication date and changes with every update, so the
link is read from the page each run.

The spreadsheet has one row per facility x animal type. Output:
    <out>/nc_deq/<original>.xlsx                      the file as published
    <out>/nc_deq/animal_facility_rows.parquet         every row, cleaned column names
    <out>/nc_deq/animal_facilities.parquet            one point per permit (EPSG:4326 assumed;
                                                      DEQ does not state the datum)
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .common import download, get, log, session, write_gdf

PAGE = ("https://www.deq.nc.gov/about/divisions/water-resources/water-quality-permitting/"
        "animal-feeding-operations/animal-facility-map")
LINK_RE = re.compile(r'href="([^"]*listpermittedanimalfacilities[^"]*)"', re.I)


def _snake(c: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(c).strip().lower()).strip("_")


def run(out: Path) -> None:
    import geopandas as gpd

    s = session()
    html = get(s, PAGE).text
    m = LINK_RE.search(html)
    if not m:
        raise RuntimeError(f"no facility-list link found on {PAGE}")
    url = m.group(1)
    if url.startswith("/"):
        url = "https://www.deq.nc.gov" + url
    name = re.sub(r"\W+", "_", url.rsplit("/", 2)[-2]) + ".xlsx"
    log(f"[NC DEQ] {url}")
    folder = Path(out) / "nc_deq"
    xlsx = download(s, url, folder / name)

    raw = pd.read_excel(xlsx, header=None)
    hdr = raw.index[raw.apply(lambda r: r.astype(str).str.strip().eq("Permit Number").any(), axis=1)][0]
    df = pd.read_excel(xlsx, header=hdr)
    df.columns = [_snake(c) for c in df.columns]
    df = df.dropna(subset=["permit_number"])
    df.attrs["source"] = url
    rows_path = folder / "animal_facility_rows.parquet"
    df.to_parquet(rows_path, index=False)
    log(f"  wrote {rows_path} ({len(df):,} rows)")

    first = ["facility_name", "combined_owner", "regulated_operation", "permit_type",
             "number_of_lagoons", "issued_date", "effective_date", "expiration_date",
             "admin_region", "county_name", "location_lat_num", "location_long_num",
             "address_1", "city", "state", "zip"]
    agg = {c: "first" for c in first if c in df}
    agg["regulated_activity"] = lambda x: "; ".join(sorted(set(map(str, x))))
    agg["allowable_count"] = "sum"
    fac = df.groupby("permit_number", as_index=False).agg(agg)
    gdf = gpd.GeoDataFrame(fac, geometry=gpd.points_from_xy(fac["location_long_num"],
                                                             fac["location_lat_num"]), crs=4326)
    write_gdf(gdf, folder / "animal_facilities.parquet")
