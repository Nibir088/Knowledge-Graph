"""California CAFOs: Water Board permit records (CIWQS) and Cal-FF (Stanford RegLab census).

1. CIWQS, "Confined Animal Facilities Regulated by the Water Boards"
   California Open Data, package surface-water-water-quality-regulated-facility-information.
   A daily CSV export of CIWQS regulatory measures in the animal-waste programs
   (ANIWSTCOWS dairies/feedlots, ANIWSTOTH poultry/other, ANIWSTGRZ grazing): one row per
   regulatory measure (WDR, general-order enrollee, NPDES, waiver, ...), Active and
   Historical, with facility_id, WDID, cafo_type/subtype, cafo_population, animal
   equivalent units and lat/lon. This is the same data as the CIWQS "Regulated Facility"
   report (program ANIMALWASTE), whose web export only gives region totals.
   Permitted facilities only.

2. Cal-FF (Hugging Face dataset reglab/cal-ff, CC0; Magesh et al., Scientific Data 2025)
   2,121 CAFOs found in satellite imagery and checked by people, about 98% of California
   CAFOs as of 2017, including ones missing from permit records. Footprint polygons,
   animal type, building count, construction/destruction date bounds and matched
   CIWQS permits. Addresses are redacted (RegLab shares them on request for research).

Output:
  <out>/ca_cafo/ciwqs_cafo_measures.parquet     every CIWQS row (all text, dates parsed)
  <out>/ca_cafo/ciwqs_cafo_facilities.parquet   one point per CIWQS facility_id (EPSG:4326);
                                                 the active measure is preferred, with
                                                 n_measures and any_active columns
  <out>/ca_cafo/cal_ff_facilities.parquet       Cal-FF footprints (MultiPolygon, EPSG:4326)
  <out>/ca_cafo/cal_ff_<table>.parquet          Cal-FF raw tables (permits, animal_typing,
                                                 construction_dating, parcel_locations, ...)
  <out>/ca_cafo/_raw/...                         files as downloaded
Point tables carry coord_ok (False = outside California, e.g. a missing minus sign on
the longitude in CIWQS); such records are kept as published. Each daily CIWQS export
has its own file name, so a later run downloads the new export and rewrites the parquet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .common import download, flag_bbox, get, log, session, write_gdf

CKAN = "https://data.ca.gov/api/3/action/package_show"
CIWQS_PACKAGE = "surface-water-water-quality-regulated-facility-information"
CIWQS_RESOURCE = "Confined Animal Facilities Regulated by the Water Boards"
HF = "https://huggingface.co/datasets/reglab/cal-ff/resolve/main/"
CALFF_FILES = ["facilities.csv", "facilities.geojson", "README.md",
               "raw/permits.csv", "raw/animal_typing.csv", "raw/construction_dating.csv",
               "raw/parcel_locations.csv", "raw/parcel_name_annotations.csv", "raw/county_groups.csv"]
CA_BBOX = (-124.5, 32.5, -114.1, 42.1)
CALFF_LARGE = ["raw/counties.csv", "raw/images.csv"]  # 11 MB, 92 MB: only with cal_ff_all


def ciwqs(out: Path) -> None:
    import geopandas as gpd

    s = session()
    folder = Path(out) / "ca_cafo"
    res = get(s, CKAN, params={"id": CIWQS_PACKAGE}).json()["result"]["resources"]
    r = next((r for r in res if r["name"].strip().lower() == CIWQS_RESOURCE.lower()), None)
    if r is None:
        raise RuntimeError(f"resource '{CIWQS_RESOURCE}' not found in {CIWQS_PACKAGE}")
    log(f"[CA CIWQS] {r['url']}")
    # file name carries the export date, so a new daily export is a new file
    csv = download(s, r["url"], folder / "_raw" / r["url"].rsplit("/", 1)[-1])

    df = pd.read_csv(csv, dtype=str, encoding="utf-8-sig", keep_default_na=False,
                     na_values=["", "NA"])
    df.columns = [c.strip().strip('"') for c in df.columns]
    for c in df.columns:
        if c.endswith("_date"):
            df[c] = pd.to_datetime(df[c], format="mixed", errors="coerce")  # ISO, some MM/DD/YYYY
    for c in ("latitude_decimal_degrees", "longitude_decimal_degrees", "cafo_population",
              "animal_equivalent_units_aeu"):
        if c in df:
            df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce")
    df.attrs["source"] = r["url"]
    path = folder / "ciwqs_cafo_measures.parquet"
    df.to_parquet(path, index=False)
    log(f"  wrote {path} ({len(df):,} rows; {(df['status'] == 'Active').sum():,} active)")

    df["_active"] = df["status"].eq("Active")
    df = df.sort_values(["facility_id", "_active", "effective_date"], ascending=[True, False, False])
    fac = df.groupby("facility_id", as_index=False).first()
    stats = df.groupby("facility_id").agg(n_measures=("reg_measure_id", "size"),
                                          any_active=("_active", "any"))
    fac = fac.drop(columns="_active").merge(stats, on="facility_id")
    fac = fac.dropna(subset=["latitude_decimal_degrees", "longitude_decimal_degrees"])
    gdf = gpd.GeoDataFrame(fac, geometry=gpd.points_from_xy(fac["longitude_decimal_degrees"],
                                                             fac["latitude_decimal_degrees"]), crs=4326)
    write_gdf(flag_bbox(gdf, CA_BBOX), folder / "ciwqs_cafo_facilities.parquet")


def _jsonify(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet cannot hold mixed list/dict columns; store them as JSON text."""
    for c in df.columns:
        if c != "geometry" and df[c].map(lambda v: isinstance(v, (list, dict))).any():
            df[c] = df[c].map(lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v)
    return df


def cal_ff(out: Path, all_files: bool = False) -> None:
    import geopandas as gpd

    s = session()
    folder = Path(out) / "ca_cafo"
    raw = folder / "_raw" / "cal_ff"
    log("[CA Cal-FF] huggingface.co/datasets/reglab/cal-ff")
    for f in CALFF_FILES + (CALFF_LARGE if all_files else []):
        download(s, HF + f, raw / f)

    g = flag_bbox(_jsonify(gpd.read_file(raw / "facilities.geojson")), CA_BBOX)
    write_gdf(g, folder / "cal_ff_facilities.parquet")
    for f in sorted((raw / "raw").glob("*.csv")):
        df = pd.read_csv(f, dtype=str, low_memory=False)
        df = df.loc[:, ~df.columns.str.startswith("Unnamed:")]
        path = folder / f"cal_ff_{f.stem}.parquet"
        df.to_parquet(path, index=False)
        log(f"  wrote {path} ({len(df):,} rows)")


def run(out: Path, sources=("ciwqs", "cal_ff"), cal_ff_all: bool = False) -> None:
    sources = [sources] if isinstance(sources, str) else list(sources)
    for src in sources:
        if src == "ciwqs":
            ciwqs(out)
        elif src == "cal_ff":
            cal_ff(out, cal_ff_all)
        else:
            raise ValueError(f"unknown ca_cafo source {src}; use ciwqs and/or cal_ff")
