"""Permitted point-source discharges: one row per facility per year, per state.

Columns (exactly these):
    npdes_id        facility (NPDES permit) ID                     key
    year            calendar year                                   key
    status          "operating" or "stopped" in that year (rule below)
    lat, lon        facility location (EPA geocode)
    facility_type   "Sewage treatment plant (POTW)", "Animal feeding operation (...)",
                    or the facility's industry (SIC description)
    flow_mgd        average wastewater flow in that year, million gallons per day
                    (sum over the facility's outfalls)
    discharges_to   receiving water name (state water body name; else the HUC12 name)
    nitrogen_kg     total nitrogen discharged that year, kg as N
    ammonia_kg      ammonia discharged that year, kg as N
    bacteria_count  all fecal-indicator bacteria discharged that year (E. coli + fecal
                    coliform + total coliform + enterococci ...), number of organisms
    total_waste_kg  all pollutants with a mass load added together, kg

Sources
  * EPA Water Pollutant Loading Tool web service (annual loads EPA computes from the
    Discharge Monitoring Reports, DMRs):
        https://echodata.epa.gov/echo/dmr_rest_services.get_custom_data_annual
    -> location, type, flow, receiving water, nitrogen, ammonia, total waste.
    Loads come in pounds/year and are converted to kg.
  * EPA DMR bulk files (one per federal fiscal year, ~0.5 GB zipped / ~9 GB of CSV):
        https://echo.epa.gov/files/echodownloads/npdes_dmrs_fy<YYYY>.zip
    -> bacteria, which the Loading Tool does not compute. The CSV is streamed and only
    the requested states' bacteria and flow rows are kept.

Rules
  nitrogen_kg     "Nitrogen, total [as N]" (00600) where reported; otherwise
                  Kjeldahl N (00625) + nitrite+nitrate (00630); otherwise empty.
                  Ammonia alone is not used as total nitrogen.
  ammonia_kg      "Nitrogen, ammonia total [as N]" (00610); else dissolved (00608).
  total_waste_kg  sum of every pollutant load in the Loading Tool's nutrient-aggregated
                  view (overlapping nitrogen forms are merged there, so nitrogen is not
                  counted twice). Other overlaps (e.g. BOD and COD) are a plain sum.
  flow_mgd        monthly average flows from the DMRs (every "Flow, ..." parameter in MGD,
                  summed over outfalls, averaged over the year); else the Loading Tool's
                  yearly average; else the permit's reported actual average flow.
  bacteria_count  per outfall and monitoring period: average concentration
                  (organisms/100 mL, effluent sampling point) x average flow (MGD; that
                  outfall's, else the facility's that month, else flow_mgd) -> organisms/day; the year's
                  value = mean over the periods reported x days in the year; summed over
                  bacteria types and outfalls, as asked. The types overlap
                  (E. coli is part of fecal coliform, which is part of total coliform),
                  so a facility reporting several types is counted several times.
  Rows: every ECHO NPDES facility of the state (echo/<ST>/ must be downloaded), one row per
  year from the year of its first permit on; pollutant columns are empty when it reported
  no monitoring data that year.
  status          "operating" if it reported monitoring data that year, or its permit was in
                  force at any time that year; "stopped" after its permit ended: the
                  termination date of a terminated permit (TRM) or the expiration date of an
                  expired, not continued one (EXP). Effective (EFF) and administratively
                  continued (ADC) permits count as in force. flow_mgd of a facility without
                  monitoring data is the permit's reported average flow, in operating years.

Output: <out>/discharges/<ST>/facility_year.parquet (+ .csv)
"""

from __future__ import annotations

import calendar
import io
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .common import download, get, log, session

LOADING_URL = "https://echodata.epa.gov/echo/dmr_rest_services.get_custom_data_annual"
DMR_URL = "https://echo.epa.gov/files/echodownloads/npdes_dmrs_fy{fy}.zip"
LB_TO_KG = 0.45359237
MG_TO_100ML = 3.785411784e6 * 10          # 1 million gallons = 3.785e9 mL = 3.785e7 x 100 mL
COLUMNS = ["npdes_id", "year", "status", "lat", "lon", "facility_type", "flow_mgd", "discharges_to",
           "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]
BACTERIA_RE = re.compile(r"coli|enterococ|coliform", re.I)
# livestock and poultry (not aquaculture 0273, horses 0272 or fur farms 0271)
LIVESTOCK_SIC = {"0211", "0212", "0213", "0214", "0219", "0241", "0251", "0252", "0253", "0254", "0259"}
FLOW_RE = re.compile(r"^flow", re.I)      # every "Flow, ..." parameter reported in MGD
BACT_LOCATIONS = ["1", "A"]                # effluent gross, then the other effluent point used in IA


# ----------------------------------------------------------------------------- Loading Tool

def loading_tool(s, st: str, year: int, agg: str, folder: Path) -> pd.DataFrame:
    """All Loading Tool rows (facility x outfall x parameter) for a state and year, cached."""
    cache = folder / "_raw" / f"loading_{st}_{year}_agg{agg}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    rows, page, pages = [], 1, 1
    while page <= pages:
        r = get(s, LOADING_URL, timeout=900, params={
            "output": "JSON", "p_st": st, "p_year": year, "p_nutrient_agg": agg,
            "responseset": 10000, "pageno": page}).json()["Results"]
        if "Error" in r:
            raise RuntimeError(f"Loading Tool {st} {year}: {r['Error']}")
        pages = int(r.get("PageCount") or 1)
        rows += r.get("Results") or []
        page += 1
    df = pd.DataFrame(rows).astype(str).replace({"None": None, "--": None, "nan": None})
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    log(f"  Loading Tool {st} {year} (nutrient aggregation {agg}): {len(df):,} rows")
    return df


def _num(x):
    return pd.to_numeric(pd.Series(x, dtype=object).astype(str).str.replace(",", ""), errors="coerce")


def facility_year_loading(raw_n: pd.DataFrame, raw_y: pd.DataFrame) -> pd.DataFrame:
    """Per facility: location, type fields, flow, receiving water, N, ammonia, total waste."""
    out = []
    by_y = dict(tuple(raw_y.groupby("ExternalPermitNmbr"))) if raw_y is not None else {}
    for pid, g in raw_n.groupby("ExternalPermitNmbr"):
        gy = by_y.get(pid, g.iloc[:0]) if raw_y is not None else g
        first = g.iloc[0]

        def total(codes, frame=g):
            d = frame[frame["ParameterCode"].isin(codes)]
            v = _num(d["PollutantLoad"])
            return float(v.sum()) * LB_TO_KG if v.notna().any() else np.nan

        tn = total(["00600"])
        if np.isnan(tn):
            tkn, no23 = total(["00625"]), total(["00630"])
            tn = tkn + no23 if not (np.isnan(tkn) or np.isnan(no23)) else np.nan
        nh3 = total(["00610"])
        if np.isnan(nh3):
            nh3 = total(["00608"])
        w = _num(gy["PollutantLoad"])
        tw = float(w.sum()) * LB_TO_KG if w.notna().any() else np.nan
        fl = g.drop_duplicates("PermFeatureNmbr")
        flow = _num(fl["AvgWastewaterFlow"])
        actual = _num(g["ActualAverageFlowNmbr"]).dropna()
        out.append({
            "npdes_id": pid,
            "lat": pd.to_numeric(first.get("GeocodeLatitude"), errors="coerce"),
            "lon": pd.to_numeric(first.get("GeocodeLongitude"), errors="coerce"),
            "_potw": first.get("FacilityTypeIndicator"), "_sic": first.get("Siccode"),
            "_flow_lt": float(flow.sum()) if flow.notna().any() else np.nan,
            "_flow_permit": float(actual.iloc[0]) if len(actual) else np.nan,
            "discharges_to": first.get("StateWaterBodyName") or
            (f"{first.get('Huc12Name')} (HUC12)" if first.get("Huc12Name") else None),
            "nitrogen_kg": tn, "ammonia_kg": nh3, "total_waste_kg": tw})
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- DMR bulk (bacteria)

DMR_COLS = ["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR", "PARAMETER_CODE", "PARAMETER_DESC",
            "MONITORING_LOCATION_CODE", "STATISTICAL_BASE_CODE", "STATISTICAL_BASE_TYPE_CODE",
            "VALUE_TYPE_CODE", "MONITORING_PERIOD_END_DATE", "DMR_VALUE_STANDARD_UNITS",
            "STANDARD_UNIT_DESC", "NODI_CODE"]


def dmr_extract(s, fy: int, states, folder: Path, keep_zip: bool = False) -> pd.DataFrame:
    """Bacteria and flow DMR values of the given states from one fiscal-year file, cached."""
    cache = folder / "_raw" / f"dmr_bacteria_flow_v2_fy{fy}_{'_'.join(sorted(states))}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    zp = download(s, DMR_URL.format(fy=fy), folder / "_raw" / "_zips" / f"npdes_dmrs_fy{fy}.zip")
    prefixes = tuple(states)
    parts = []
    with zipfile.ZipFile(zp) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(member) as fh:
            for chunk in pd.read_csv(io.TextIOWrapper(fh, encoding="latin-1"), usecols=DMR_COLS, dtype=str,
                                     chunksize=1_000_000, keep_default_na=False, na_values=[""]):
                c = chunk[chunk["EXTERNAL_PERMIT_NMBR"].str[:2].isin(prefixes)]
                c = c[c["PARAMETER_DESC"].str.contains(FLOW_RE, na=False)
                      | c["PARAMETER_DESC"].str.contains(BACTERIA_RE, na=False)]
                if len(c):
                    parts.append(c)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=DMR_COLS)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    log(f"  DMR FY{fy}: {len(df):,} bacteria/flow rows for {', '.join(states)}")
    if not keep_zip:
        zp.unlink(missing_ok=True)
    return df


# averaging statistics, monthly ones first (a weekly average is the worst week, so higher)
AVG_ORDER = ["MK", "MM", "GA", "3A", "3C", "M3", "DB", "DA", "7C", "WK"]


def _pick_avg(df: pd.DataFrame, keys) -> pd.DataFrame:
    """One averaging value per keys: the monthly average where present, else another average."""
    d = df[(df["STATISTICAL_BASE_TYPE_CODE"] == "AVG") & df["value"].notna()].copy()
    d["rank"] = d["STATISTICAL_BASE_CODE"].map({c: i for i, c in enumerate(AVG_ORDER)}).fillna(len(AVG_ORDER))
    return d.sort_values("rank").drop_duplicates(keys)


def _prep(dmr: pd.DataFrame) -> pd.DataFrame:
    d = dmr.copy()
    d["value"] = pd.to_numeric(d["DMR_VALUE_STANDARD_UNITS"], errors="coerce")
    d["end"] = pd.to_datetime(d["MONITORING_PERIOD_END_DATE"], format="%m/%d/%Y", errors="coerce")
    d = d.dropna(subset=["end"])
    d["year"] = d["end"].dt.year
    return d


KEY = ["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR", "end"]


def dmr_flows(d: pd.DataFrame) -> pd.DataFrame:
    """One average flow (MGD) per permit, outfall and monitoring period."""
    f = d[d["PARAMETER_DESC"].str.contains(FLOW_RE, na=False) & (d["STANDARD_UNIT_DESC"] == "MGD")]
    f = f.assign(loc_rank=(f["MONITORING_LOCATION_CODE"] != "1").astype(int)).sort_values("loc_rank")
    return _pick_avg(f, KEY)[KEY + ["year", "value"]].rename(columns={"value": "flow"})


def facility_flow(flows: pd.DataFrame) -> pd.DataFrame:
    """npdes_id, year, flow_mgd: per period the sum over outfalls, averaged over the year."""
    per = flows.groupby(["EXTERNAL_PERMIT_NMBR", "year", "end"])["flow"].sum()
    out = per.groupby(level=[0, 1]).mean().reset_index()
    return out.rename(columns={"EXTERNAL_PERMIT_NMBR": "npdes_id", "flow": "flow_mgd"})


def bacteria_per_year(d: pd.DataFrame, flows: pd.DataFrame, year_flow: pd.Series) -> pd.DataFrame:
    """npdes_id, year, bacteria_count (see module docstring).

    year_flow: final flow_mgd per (npdes_id, year), used when a period has no outfall or
    facility flow of its own.
    """
    permit_flow = flows.groupby(["EXTERNAL_PERMIT_NMBR", "end"])["flow"].sum().rename("flow_permit")
    b = d[~d["PARAMETER_DESC"].str.contains(FLOW_RE, na=False) & d["PARAMETER_DESC"].str.contains(BACTERIA_RE, na=False)
          & ~d["PARAMETER_DESC"].str.contains("%", na=False) & d["STANDARD_UNIT_DESC"].str.contains("100", na=False)
          & d["MONITORING_LOCATION_CODE"].isin(BACT_LOCATIONS)]
    b = b.assign(loc_rank=b["MONITORING_LOCATION_CODE"].map({c: i for i, c in enumerate(BACT_LOCATIONS)}))
    b = _pick_avg(b.sort_values("loc_rank"), KEY + ["PARAMETER_CODE"])[KEY + ["year", "PARAMETER_CODE", "value"]]
    b = b.merge(flows[KEY + ["flow"]], on=KEY, how="left").join(permit_flow, on=["EXTERNAL_PERMIT_NMBR", "end"])
    b = b.join(year_flow.rename("flow_year"), on=["EXTERNAL_PERMIT_NMBR", "year"])
    q = b["flow"].fillna(b["flow_permit"]).fillna(b["flow_year"])
    b["per_day"] = b["value"] * q * MG_TO_100ML                 # organisms per day
    per = b.dropna(subset=["per_day"]).groupby(
        ["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR", "PARAMETER_CODE", "year"])["per_day"].mean().reset_index()
    per["days"] = [366 if calendar.isleap(int(y)) else 365 for y in per["year"]]
    per["count"] = per["per_day"] * per["days"]
    out = per.groupby(["EXTERNAL_PERMIT_NMBR", "year"])["count"].sum().reset_index()
    return out.rename(columns={"EXTERNAL_PERMIT_NMBR": "npdes_id", "count": "bacteria_count"})


# ----------------------------------------------------------------------------- facility type

def facility_type(potw, sic, sic_desc: dict) -> str:
    if str(potw).upper() == "POTW":
        return "Sewage treatment plant (POTW)"
    s = str(sic or "").strip()
    d = sic_desc.get(s)
    if s in LIVESTOCK_SIC:
        return f"Animal feeding operation ({d or 'SIC ' + s})"
    return d or (f"SIC {s}" if s and s != "None" else "Other / unknown")


def sic_lookup(raw: Path, st: str) -> dict:
    f = raw / "echo" / st / "NPDES_SICS.parquet"
    if not f.exists():
        return {}
    t = pd.read_parquet(f, columns=["SIC_CODE", "SIC_DESC"]).dropna().drop_duplicates("SIC_CODE")
    return dict(zip(t["SIC_CODE"].astype(str), t["SIC_DESC"].str.strip().str.capitalize()))


def _date(s):
    return pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")


def echo_facilities(raw: Path, st: str, sic_desc: dict):
    """Every ECHO facility of the state, indexed by NPDES ID, with
    lat, lon, facility_type, discharges_to, flow_permit, start, end (see `status` rule)."""
    f = raw / "echo" / st / "ICIS_FACILITIES.parquet"
    if not f.exists():
        return None
    e = raw / "echo" / st
    fac = pd.read_parquet(f, columns=["NPDES_ID", "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE"]).drop_duplicates("NPDES_ID")
    out = pd.DataFrame({"lat": pd.to_numeric(fac["GEOCODE_LATITUDE"], errors="coerce").values,
                        "lon": pd.to_numeric(fac["GEOCODE_LONGITUDE"], errors="coerce").values}, index=fac["NPDES_ID"].values)
    perm = pd.read_parquet(e / "ICIS_PERMITS.parquet") if (e / "ICIS_PERMITS.parquet").exists() else None
    comps = {}
    if (e / "NPDES_PERM_COMPONENTS.parquet").exists():
        c = pd.read_parquet(e / "NPDES_PERM_COMPONENTS.parquet", columns=["EXTERNAL_PERMIT_NMBR", "COMPONENT_TYPE_CODE"])
        comps = c.groupby("EXTERNAL_PERMIT_NMBR")["COMPONENT_TYPE_CODE"].agg(set).to_dict()
    sic = {}
    if (e / "NPDES_SICS.parquet").exists():
        t = pd.read_parquet(e / "NPDES_SICS.parquet", columns=["NPDES_ID", "SIC_CODE", "PRIMARY_INDICATOR_FLAG"])
        sic = t.sort_values("PRIMARY_INDICATOR_FLAG", ascending=False).drop_duplicates("NPDES_ID") \
               .set_index("NPDES_ID")["SIC_CODE"].to_dict()
    potw, out["discharges_to"], out["flow_permit"], out["start"], out["end"] = {}, None, np.nan, pd.NaT, pd.NaT
    if perm is not None:
        perm = perm.assign(v=pd.to_numeric(perm["VERSION_NMBR"], errors="coerce"))
        first = perm.assign(d=_date(perm["ORIGINAL_ISSUE_DATE"]).fillna(_date(perm["EFFECTIVE_DATE"]))) \
                    .groupby("EXTERNAL_PERMIT_NMBR")["d"].min()
        last = perm.sort_values("v").drop_duplicates("EXTERNAL_PERMIT_NMBR", keep="last").set_index("EXTERNAL_PERMIT_NMBR")
        # stopped from: termination date of a terminated permit, expiration date of an expired one
        end = pd.Series(pd.NaT, index=last.index)
        trm, exp = last["PERMIT_STATUS_CODE"] == "TRM", last["PERMIT_STATUS_CODE"] == "EXP"
        end[trm] = _date(last.loc[trm, "TERMINATION_DATE"]).fillna(_date(last.loc[trm, "EXPIRATION_DATE"]))
        end[exp] = _date(last.loc[exp, "EXPIRATION_DATE"])
        out["start"] = out.index.map(first)
        out["end"] = out.index.map(end)
        out["discharges_to"] = out.index.map(last["STATE_WATER_BODY_NAME"])
        out["flow_permit"] = out.index.map(pd.to_numeric(last["ACTUAL_AVERAGE_FLOW_NMBR"], errors="coerce"))
        potw = last["FACILITY_TYPE_INDICATOR"].to_dict()
    out["facility_type"] = [facility_type_echo(i, potw.get(i), comps.get(i, set()), sic.get(i), sic_desc)
                            for i in out.index]
    return out


def facility_type_echo(npdes_id, potw, comps: set, sic, sic_desc: dict) -> str:
    """Type from the POTW flag, the permit components and the industry code, in that order."""
    s = str(sic or "")
    if str(potw).upper() == "POTW" or "POT" in comps:
        return "Sewage treatment plant (POTW)"
    if "CAF" in comps or s in LIVESTOCK_SIC:
        return f"Animal feeding operation ({sic_desc.get(s) or 'SIC ' + s if s else 'CAFO'})"
    if "SWC" in comps or str(npdes_id).upper().startswith("NCC"):   # NCC: NC construction coverages
        return "Construction site stormwater"
    if "SWS" in comps or "SWM" in comps:
        return "Municipal stormwater system (MS4)"
    if "SWI" in comps:
        return f"Industrial stormwater ({sic_desc[s]})" if s in sic_desc else "Industrial stormwater"
    if comps == {"BIO"}:
        return "Biosolids"
    return facility_type(None, sic, sic_desc)


# ----------------------------------------------------------------------------- run

def run(out: Path, states=("NC", "CA", "IA"), start_year: int = 2017, end_year: int = 2024,
        keep_zip: bool = False) -> None:
    s = session()
    out = Path(out)
    root = out / "discharges"
    states = [x.upper() for x in ([states] if isinstance(states, str) else states)]
    years = list(range(int(start_year), int(end_year) + 1))
    # a federal fiscal year runs Oct-Sep: calendar years Y need FY Y and FY Y+1
    dmr = []
    for fy in range(years[0], years[-1] + 2):
        log(f"[discharges] DMR fiscal year {fy}")
        try:
            dmr.append(dmr_extract(s, fy, states, root, keep_zip))
        except Exception as exc:  # noqa: BLE001 - e.g. the newest FY not published yet
            log(f"  FY{fy} not available ({exc}); bacteria/flow for the months it covers stay empty")
    d = _prep(pd.concat(dmr, ignore_index=True) if dmr else pd.DataFrame(columns=DMR_COLS))
    d = d[d["year"].isin(years)]
    flows = dmr_flows(d)
    ff = facility_flow(flows)
    for st in states:
        sic_desc = sic_lookup(out, st)
        rows = []
        for y in years:
            log(f"[discharges] {st} {y}")
            fy = facility_year_loading(loading_tool(s, st, y, "N", root), loading_tool(s, st, y, "Y", root))
            fy["year"] = y
            rows.append(fy)
        df = pd.concat(rows, ignore_index=True)
        df["facility_type"] = [facility_type(p, c, sic_desc) for p, c in zip(df["_potw"], df["_sic"])]
        # flow: monthly DMR flows, else the Loading Tool's yearly average, else the permit's average flow
        df = df.merge(ff[ff["npdes_id"].str[:2] == st], on=["npdes_id", "year"], how="outer")
        df["flow_mgd"] = df["flow_mgd"].fillna(df["_flow_lt"]).fillna(df["_flow_permit"])
        yf = df.set_index(["npdes_id", "year"])["flow_mgd"].dropna()
        yf = yf[~yf.index.duplicated()]
        dst = d[d["EXTERNAL_PERMIT_NMBR"].str[:2] == st]
        b = bacteria_per_year(dst, flows[flows["EXTERNAL_PERMIT_NMBR"].str[:2] == st], yf)
        df = df.merge(b, on=["npdes_id", "year"], how="outer")
        df["status"] = "operating"                     # it reported monitoring data that year
        # every other ECHO facility: one row per year from its first permit on, with status
        echo = echo_facilities(out, st, sic_desc)
        if echo is not None:
            have = set(zip(df["npdes_id"], df["year"]))
            add = []
            for i, r in echo.iterrows():
                y0 = r["start"].year if pd.notna(r["start"]) else years[0]
                for y in years:
                    if y < y0 or (i, y) in have:
                        continue
                    op = pd.isna(r["end"]) or r["end"] >= pd.Timestamp(y, 1, 1)
                    add.append((i, y, "operating" if op else "stopped", r["flow_permit"] if op else np.nan))
            df = pd.concat([df, pd.DataFrame(add, columns=["npdes_id", "year", "status", "flow_mgd"])],
                           ignore_index=True)
            # location / type / receiving water: ECHO's where known (same for every year)
            for c in ("lat", "lon", "facility_type", "discharges_to"):
                df[c] = df["npdes_id"].map(echo[c]).where(df["npdes_id"].isin(echo.index) & df["npdes_id"].map(echo[c]).notna(), df[c])
        fixed = df.dropna(subset=["lat"]).drop_duplicates("npdes_id", keep="last").set_index("npdes_id")
        for c in ("lat", "lon", "facility_type", "discharges_to"):
            df[c] = df[c].fillna(df["npdes_id"].map(fixed[c]))
        df = df[COLUMNS].sort_values(["npdes_id", "year"]).reset_index(drop=True)
        dest = root / st / "facility_year.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest, index=False)
        df.to_csv(dest.with_suffix(".csv"), index=False)
        log(f"  wrote {dest} ({len(df):,} facility-years, {df['npdes_id'].nunique():,} facilities)")
