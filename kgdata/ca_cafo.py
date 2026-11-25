"""California CAFOs: Water Board permits (CIWQS), Cal-FF (Stanford RegLab) and CARB CADD.

1. CIWQS, "Confined Animal Facilities Regulated by the Water Boards"
   California Open Data, package surface-water-water-quality-regulated-facility-information.
   A daily CSV export of CIWQS regulatory measures in the animal-waste programs
   (ANIWSTCOWS dairies/feedlots, ANIWSTOTH poultry/other, ANIWSTGRZ grazing): one row per
   regulatory measure (WDR, general-order enrollee, NPDES, waiver, ...), Active and
   Historical, with facility_id, WDID, cafo_type/subtype, cafo_population, animal
   equivalent units and lat/lon. Permitted facilities only.

1b. CIWQS "Regulated Facility" report (ciwqs.waterboards.ca.gov), program ANIMALWASTE.
   The same permits as the web page users see: the report is run, each statewide
   drill-down (ANIWSTCOWS, ANIWSTOTH, ANIWSTGRZ) is opened and its Excel (tab-separated)
   export downloaded. Adds columns the open-data file lacks: owner/agency, facility
   address, complexity, threat-to-water-quality (TTWQ) and the number of enforcement
   actions and violations in the last 5 years. One row per facility x regulatory measure.

2. Cal-FF (Hugging Face dataset reglab/cal-ff, CC0; Magesh et al., Scientific Data 2025)
   2,121 CAFOs found in satellite imagery and checked by people, about 98% of California
   CAFOs as of 2017, including ones missing from permit records. Footprint polygons,
   animal type, building count, construction/destruction date bounds and matched
   CIWQS permits. Addresses are redacted (RegLab shares them on request for research).

3. CARB CADD, California Dairy & Livestock Database (ww2.arb.ca.gov). Facility-level
   cattle populations and digester information for CA dairies, by year from 2012, in one
   Excel workbook. The newest "CADD_v<version>...xlsx" linked from CARB's CADD pages is
   downloaded and every sheet written to Parquet. CARB's website rate-limits automated
   clients; if it refuses, download the workbook in a browser and put it in
   <out>/ca_cafo/_raw/cadd/ (any *.xlsx there is used), or set cadd_url.

Output:
  <out>/ca_cafo/ciwqs_cafo_measures.parquet     every CIWQS row (all text, dates parsed)
  <out>/ca_cafo/ciwqs_cafo_facilities.parquet   one point per CIWQS facility_id (EPSG:4326);
                                                 the active measure is preferred, with
                                                 n_measures and any_active columns
  <out>/ca_cafo/cal_ff_facilities.parquet       Cal-FF footprints (MultiPolygon, EPSG:4326)
  <out>/ca_cafo/cal_ff_<table>.parquet          Cal-FF raw tables (permits, animal_typing,
                                                 construction_dating, parcel_locations, ...)
  <out>/ca_cafo/ciwqs_report_rows.parquet       CIWQS report, every row
  <out>/ca_cafo/ciwqs_report_facilities.parquet the rows with coordinates, as points (EPSG:4326)
  <out>/ca_cafo/cadd_<sheet>.parquet            one file per CADD workbook sheet
  <out>/ca_cafo/_raw/...                         files as downloaded
Point tables carry coord_ok (False = outside California, e.g. a missing minus sign on
the longitude in CIWQS); such records are kept as published. Each daily CIWQS export
has its own file name, so a later run downloads the new export and rewrites the parquet.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html import unescape
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
CIWQS_SERVLET = "https://ciwqs.waterboards.ca.gov/ciwqs/readOnly/CiwqsReportServlet"
STATEWIDE_RE = re.compile(r'href="(CiwqsReportServlet\?inCommand=drilldown&(?:amp;)?reportName='
                          r'RegulatedFacilityDetail&(?:amp;)?program=ANIMALWASTE&(?:amp;)?'
                          r'subprogram=(\w+)&(?:amp;)?reportID=\d+)"', re.I)
EXPORT_RE = re.compile(r'(CiwqsReportServlet\?reportID=\d+&(?:amp;)?exportToExcel=Y)', re.I)
CADD_PAGES = ["https://ww2.arb.ca.gov/resources/documents/california-dairy-livestock-database-cadd",
              "https://ww2.arb.ca.gov/resources/documents/california-dairy-livestock-database-cadd-supporting-documents"]
XLSX_RE = re.compile(r'href="([^"]+\.xlsx)"', re.I)


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


def ciwqs_report(out: Path, status: str = "Active") -> None:
    """Run the CIWQS Regulated Facility report (ANIMALWASTE) and export each statewide program.

    status: "Active", "Historical/Terminated" or "" (any).
    """
    import geopandas as gpd

    s = session()  # the servlet keeps the report in the session cookie
    folder = Path(out) / "ca_cafo"
    raw = folder / "_raw" / f"ciwqs_report_{date.today():%Y%m%d}"
    raw.mkdir(parents=True, exist_ok=True)
    log(f"[CA CIWQS report] program ANIMALWASTE, status {status or 'any'}")
    html = get(s, CIWQS_SERVLET, method="POST", data={
        "reportName": "RegulatedFacility", "groupType": "0", "programDrop": "ANIMALWASTE",
        "inStatus": status, "enpRepButton": "Run Report"}).text
    links = {sp: unescape(href) for href, sp in STATEWIDE_RE.findall(html)}
    if not links:
        raise RuntimeError("CIWQS report returned no statewide drill-down links")
    frames = []
    for sp, href in sorted(links.items()):
        page = get(s, CIWQS_SERVLET.rsplit("/", 1)[0] + "/" + href).text
        m = EXPORT_RE.search(page)
        if not m:
            raise RuntimeError(f"no Excel export link on the CIWQS {sp} page")
        tsv = raw / f"{sp}.tsv"
        tsv.write_bytes(get(s, CIWQS_SERVLET.rsplit("/", 1)[0] + "/" + unescape(m.group(1))).content)
        df = pd.read_csv(tsv, sep="\t", dtype=str, encoding="cp1252", keep_default_na=False,
                         na_values=["", "null"])
        df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
        df = df.apply(lambda c: c.str.strip())
        df.insert(0, "subprogram", sp)
        log(f"  {sp}: {len(df):,} rows, {df['WDID'].nunique():,} WDIDs")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df.columns = [re.sub(r"[^0-9a-z]+", "_", c.lower()).strip("_") for c in df.columns]
    for c in ("latitude", "longitude", "caf_population", "aeu", "design_flow",
              "enforcement_actions_within_5_years", "violations_within_5_years"):
        if c in df:
            df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce")
    for c in ("adoption_date", "effective_date", "termination_date", "expiration_review_date"):
        if c in df:
            df[c] = pd.to_datetime(df[c], format="%m/%d/%Y", errors="coerce")
    rows_path = folder / "ciwqs_report_rows.parquet"
    df.to_parquet(rows_path, index=False)
    log(f"  wrote {rows_path} ({len(df):,} rows)")
    pts = df.dropna(subset=["latitude", "longitude"])
    if len(pts) < len(df):
        log(f"    {len(df) - len(pts)} rows without coordinates are left out of the points file")
    gdf = gpd.GeoDataFrame(pts, geometry=gpd.points_from_xy(pts["longitude"], pts["latitude"]), crs=4326)
    write_gdf(flag_bbox(gdf, CA_BBOX), folder / "ciwqs_report_facilities.parquet")


def _cadd_version(url: str) -> tuple:
    m = re.search(r"v(\d+)[._](\d+)[._](\d+)", url, re.I)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def cadd(out: Path, url: str | None = None) -> None:
    """CARB California Dairy & Livestock Database: newest public workbook, one Parquet per sheet."""
    s = session()
    folder = Path(out) / "ca_cafo"
    raw = folder / "_raw" / "cadd"
    local = sorted(raw.glob("*.xlsx")) if raw.exists() else []
    if url:
        xlsx = download(s, url, raw / unescape(url).rsplit("/", 1)[-1].replace("%20", "_"))
    elif local:
        xlsx = max(local, key=lambda p: _cadd_version(p.name))
        log(f"[CA CADD] using {xlsx}")
    else:
        found = set()
        for page in CADD_PAGES:
            try:
                html = get(s, page, retries=2).text
            except Exception as exc:  # noqa: BLE001
                log(f"    {page}: {exc}")
                continue
            found |= {unescape(h) for h in XLSX_RE.findall(html) if "cadd" in h.lower()}
        if not found:
            raise RuntimeError(
                "no CADD workbook link found (CARB's site may be rate-limiting this client). "
                f"Download it from {CADD_PAGES[0]} in a browser and put it in {raw}/, "
                "or set cadd_url in the config.")
        best = max(found, key=_cadd_version)
        if best.startswith("/"):
            best = "https://ww2.arb.ca.gov" + best
        log(f"[CA CADD] {best}")
        xlsx = download(s, best, raw / best.rsplit("/", 1)[-1].replace("%20", "_"))

    sheets = pd.read_excel(xlsx, sheet_name=None)
    for name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            continue
        df.columns = [re.sub(r"[^0-9a-z]+", "_", str(c).lower()).strip("_") for c in df.columns]
        for c in df.columns:  # mixed text/number columns cannot go to Parquet as-is
            if df[c].dtype == object:
                df[c] = df[c].map(lambda v: v if v is None or isinstance(v, str) or pd.isna(v) else str(v))
        stem = re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")
        path = folder / f"cadd_{stem}.parquet"
        df.to_parquet(path, index=False)
        log(f"  wrote {path} ({len(df):,} rows) from sheet '{name}'")


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


def run(out: Path, sources=("ciwqs", "ciwqs_report", "cal_ff", "cadd"), cal_ff_all: bool = False,
        ciwqs_report_status: str = "Active", cadd_url: str | None = None) -> None:
    sources = [sources] if isinstance(sources, str) else list(sources)
    failed = []
    for src in sources:
        try:
            if src == "ciwqs":
                ciwqs(out)
            elif src == "ciwqs_report":
                ciwqs_report(out, ciwqs_report_status)
            elif src == "cal_ff":
                cal_ff(out, cal_ff_all)
            elif src == "cadd":
                cadd(out, cadd_url)
            else:
                raise ValueError(f"unknown ca_cafo source {src}; use ciwqs, ciwqs_report, cal_ff, cadd")
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - one CA source failing should not stop the rest
            log(f"  [ca_cafo:{src}] FAILED: {exc}")
            failed.append(src)
    if failed:
        raise RuntimeError(f"ca_cafo sources failed: {failed}")
