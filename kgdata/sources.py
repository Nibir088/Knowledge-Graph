"""Pollution source list: CAFOs, sewage treatment plants and industrial facilities, per year.

Built from data already downloaded (no downloads):
    raw/discharges/<ST>/facility_year.parquet   NPDES facilities per year (kgdata.discharges)
    raw/echo/<ST>/                              industry codes and permit types
    raw/nc_deq/, raw/ia_dnr/, raw/ca_cafo/      state CAFO lists

Output: raw/sources/<ST>/source_year.parquet (+ .csv), one row per source per year.

Columns
    source_id       NPDES ID for plants/facilities; NC DEQ permit number (NC), "IA-AFO-<progid>"
                    (Iowa DNR) or "CA-CALFF-<facility_id>" (Cal-FF) for CAFOs
    year
    source_type     "CAFO" | "Sewage treatment plant" | "Industrial facility"
    status          "operating" | "stopped" in that year
    discharge       "measured" (flow or pollutant values reported that year) |
                    "unknown" (operating, no discharge data) | "none" (stopped)
    lat, lon
    detail          industry (SIC description) for facilities; animal type for CAFOs
    animal_count    CAFOs: NC allowable count; Iowa DNR sum of head counts; Cal-FF has none
    npdes_id        the NPDES permit when the source has one (CAFOs: matched, see below)
    flow_mgd, discharges_to, nitrogen_kg, ammonia_kg, bacteria_count, total_waste_kg
                    from the discharges dataset (empty unless measured)

Classification of NPDES facilities (by ECHO industry code, SIC, where available)
    CAFO                    livestock/poultry SIC 0211-0219, 0241, 0251-0259 (not aquaculture 0273)
    Sewage treatment plant  POTWs, plus private domestic-sewage plants: SIC 4952 sewerage systems,
                            6513/6514 apartments and dwellings, 6515 mobile-home parks,
                            6552 subdivisions, 7011 hotels, 7032/7033 camps and RV parks,
                            8051/8059 nursing homes, 8211 schools, 8221/8222 colleges,
                            8361 residential care, 9223 correctional institutions
    left out                construction sites (and SIC 15-17), industrial-stormwater-only,
                            municipal stormwater (MS4) and biosolids-only permits
    Industrial facility     every other NPDES facility; one of unknown type only when it
                            reported a discharge
CAFOs from the state lists are the CAFO nodes. NPDES CAFO permits are attached to them (NC by
permit number, IA/CA by nearest listed CAFO within 500 m); unmatched ones are added.
CAFO status: NC DEQ list = operating (permitted now); Iowa DNR "Inactive" = stopped (the year
it stopped is not given); Cal-FF: not yet built before its construction date (no row),
stopped after its destruction date, else operating.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .common import log

CAFO_SIC = {"0211", "0212", "0213", "0214", "0219", "0241", "0251", "0252", "0253", "0254", "0259"}
DOMESTIC_SIC = {"4952", "6513", "6514", "6515", "6552", "7011", "7032", "7033", "8051", "8059",
                "8211", "8221", "8222", "8361", "9223"}
EXCLUDE_PREFIX = ("Construction site", "Industrial stormwater", "Municipal stormwater", "Biosolids")
COLUMNS = ["source_id", "year", "source_type", "status", "discharge", "lat", "lon", "detail", "animal_count",
           "npdes_id", "flow_mgd", "discharges_to", "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]
VALUES = ["flow_mgd", "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]
IA_SPECIES = {"Swine": "swine", "CattleDair": "dairy cattle", "CattleBeef": "beef cattle", "Chickens": "chickens",
              "Turkeys": "turkeys", "SheepLGoat": "sheep/goats", "Horses": "horses", "Other": "other"}


def _sic_codes(raw: Path, st: str) -> dict:
    f = raw / "echo" / st / "NPDES_SICS.parquet"
    if not f.exists():
        return {}
    t = pd.read_parquet(f, columns=["NPDES_ID", "SIC_CODE", "PRIMARY_INDICATOR_FLAG"])
    t = t.sort_values("PRIMARY_INDICATOR_FLAG", ascending=False).drop_duplicates("NPDES_ID")
    return dict(zip(t["NPDES_ID"], t["SIC_CODE"].astype(str)))


def classify(ftype, sic, measured) -> str | None:
    t = "" if pd.isna(ftype) else str(ftype)
    sic = "" if pd.isna(sic) else str(sic)
    if sic in CAFO_SIC or (t.startswith("Animal feeding operation") and "aquaculture" not in t.lower()):
        return "CAFO"
    if t.startswith(EXCLUDE_PREFIX) or sic[:2] in ("15", "16", "17"):
        return None
    if t.startswith("Sewage treatment plant") or sic in DOMESTIC_SIC:
        return "Sewage treatment plant"
    if t in ("Other / unknown", "") and not measured:
        return None
    return "Industrial facility"


def _detail(ftype: str) -> str:
    t = str(ftype or "")
    if t.startswith("Animal feeding operation (") and t.endswith(")"):
        return t[len("Animal feeding operation ("):-1]
    return t


# ----------------------------------------------------------------------------- CAFO lists

def cafo_list(raw: Path, st: str, years) -> pd.DataFrame | None:
    import geopandas as gpd
    rows = []
    if st == "NC" and (raw / "nc_deq" / "animal_facilities.parquet").exists():
        g = gpd.read_parquet(raw / "nc_deq" / "animal_facilities.parquet").to_crs(4326)
        base = pd.DataFrame({"source_id": g["permit_number"].astype(str), "lat": g.geometry.y, "lon": g.geometry.x,
                             "detail": g["regulated_operation"].astype(str).str.strip(),
                             "animal_count": pd.to_numeric(g["allowable_count"], errors="coerce")})
        for y in years:
            rows.append(base.assign(year=y, status="operating"))
    elif st == "IA" and (raw / "ia_dnr" / "afo_facilities.parquet").exists():
        g = gpd.read_parquet(raw / "ia_dnr" / "afo_facilities.parquet").to_crs(4326)
        heads = g[[c for c in IA_SPECIES if c in g]].apply(pd.to_numeric, errors="coerce")
        main = heads.fillna(0).idxmax(axis=1).map(IA_SPECIES).where(heads.fillna(0).max(axis=1) > 0, "unknown")
        base = pd.DataFrame({"source_id": "IA-AFO-" + g["progid"].astype(str), "lat": g.geometry.y, "lon": g.geometry.x,
                             "detail": g["OperatType"].fillna("").astype(str) + " (" + main + ")",
                             "animal_count": heads.sum(axis=1, min_count=1)})
        stopped = g["opStatus"].astype(str).str.lower().eq("inactive").values
        for y in years:
            rows.append(base.assign(year=y, status=np.where(stopped, "stopped", "operating")))
    elif st == "CA" and (raw / "ca_cafo" / "cal_ff_facilities.parquet").exists():
        g = gpd.read_parquet(raw / "ca_cafo" / "cal_ff_facilities.parquet").to_crs(4326)
        pt = g.geometry.representative_point()
        built = pd.to_datetime(g["construction_lower"], errors="coerce")
        gone = pd.to_datetime(g["destruction_upper"], errors="coerce")
        base = pd.DataFrame({"source_id": "CA-CALFF-" + g["facility_id"].astype(str), "lat": pt.y, "lon": pt.x,
                             "detail": g["animal_type"].astype(str), "animal_count": np.nan})
        for y in years:
            keep = ~(built > pd.Timestamp(y, 12, 31))
            st_ = np.where(gone < pd.Timestamp(y, 1, 1), "stopped", "operating")
            rows.append(base.assign(year=y, status=st_)[keep.values])
    if not rows:
        return None
    c = pd.concat(rows, ignore_index=True)
    c["source_type"] = "CAFO"
    return c


def _nearest(a: pd.DataFrame, b: pd.DataFrame, max_m: float = 500):
    """For each row of a (lat/lon), the index of the nearest row of b within max_m, else None."""
    import geopandas as gpd
    ga = gpd.GeoDataFrame(a[[]], geometry=gpd.points_from_xy(a["lon"], a["lat"]), crs=4326).to_crs(5070)
    gb = gpd.GeoDataFrame(b[["source_id"]], geometry=gpd.points_from_xy(b["lon"], b["lat"]), crs=4326).to_crs(5070)
    j = gpd.sjoin_nearest(ga, gb, max_distance=max_m, how="left")
    return j[~j.index.duplicated()]["source_id"].reindex(ga.index)


# ----------------------------------------------------------------------------- run

def run(raw: Path, states=("NC", "CA", "IA"), start_year: int = 2017, end_year: int = 2024) -> None:
    raw = Path(raw)
    years = list(range(int(start_year), int(end_year) + 1))
    for st in [s.upper() for s in ([states] if isinstance(states, str) else states)]:
        f = raw / "discharges" / st / "facility_year.parquet"
        if not f.exists():
            log(f"[sources] {st}: {f} not found (run the discharges step first); skipped")
            continue
        log(f"[sources] {st}")
        d = pd.read_parquet(f)
        d = d[d["year"].between(years[0], years[-1])].copy()
        sic = _sic_codes(raw, st)
        d["_sic"] = d["npdes_id"].map(sic)
        d["_measured"] = d[VALUES].notna().any(axis=1)
        d["source_type"] = [classify(t, s, m) for t, s, m in zip(d["facility_type"], d["_sic"], d["_measured"])]
        if st == "NC" and (raw / "nc_deq" / "animal_facilities.parquet").exists():
            # an NPDES ID that is also an NC DEQ animal permit is a CAFO, whatever its ECHO type
            nc_ids = set(pd.read_parquet(raw / "nc_deq" / "animal_facilities.parquet", columns=["permit_number"])
                         ["permit_number"].astype(str))
            d.loc[d["npdes_id"].isin(nc_ids), "source_type"] = "CAFO"
        dropped = d["source_type"].isna()
        log(f"  NPDES facility-years left out (construction, stormwater-only, MS4, biosolids, unknown without "
            f"data): {int(dropped.sum()):,} of {len(d):,}")
        d = d[~dropped]
        d["source_id"] = d["npdes_id"]
        d["detail"] = d["facility_type"].map(_detail)
        d["animal_count"] = np.nan
        fac = d[d["source_type"] != "CAFO"]
        npdes_cafo = d[d["source_type"] == "CAFO"]

        cafo = cafo_list(raw, st, years)
        if cafo is not None and len(npdes_cafo):
            # attach NPDES CAFO permits to listed CAFOs: NC by permit number, else nearest within 500 m
            ids = npdes_cafo.drop_duplicates("npdes_id")
            if st == "NC":
                match = ids["npdes_id"].where(ids["npdes_id"].isin(set(cafo["source_id"])))
            else:
                match = _nearest(ids, cafo.drop_duplicates("source_id"))
            m = dict(zip(ids["npdes_id"], match.values))
            npdes_cafo = npdes_cafo.assign(_to=npdes_cafo["npdes_id"].map(m))
            hit = npdes_cafo[npdes_cafo["_to"].notna()]
            vals = hit.set_index(["_to", "year"])[VALUES + ["discharges_to", "npdes_id"]]
            vals = vals[~vals.index.duplicated()]
            cafo = cafo.join(vals, on=["source_id", "year"])
            extra = npdes_cafo[npdes_cafo["_to"].isna()].drop(columns="_to")
            log(f"  NPDES CAFO permits: {ids['npdes_id'].nunique():,}; matched to listed CAFOs "
                f"{int(match.notna().sum()):,}; added as their own CAFO nodes {extra['npdes_id'].nunique():,}")
            cafo = pd.concat([cafo, extra], ignore_index=True)
        elif cafo is None:
            cafo = npdes_cafo
            log("  no state CAFO list on disk: CAFOs are the NPDES CAFO permits only")
        out = pd.concat([fac, cafo], ignore_index=True)
        for c in COLUMNS:
            if c not in out:
                out[c] = np.nan
        measured = out[VALUES].notna().any(axis=1)
        out["discharge"] = np.where(out["status"] == "stopped", "none", np.where(measured, "measured", "unknown"))
        out = out[COLUMNS].sort_values(["source_type", "source_id", "year"]).reset_index(drop=True)
        dest = raw / "sources" / st / "source_year.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(dest, index=False)
        out.to_csv(dest.with_suffix(".csv"), index=False)
        last = out[out["year"] == out["year"].max()]
        summary = last.groupby(["source_type", "discharge"]).size().unstack(fill_value=0)
        log(f"  wrote {dest} ({len(out):,} rows); in {int(out['year'].max())}:\n" + summary.to_string())
