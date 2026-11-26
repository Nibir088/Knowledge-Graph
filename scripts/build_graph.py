#!/usr/bin/env python3
"""Build the hydrological knowledge graph for one state from the downloaded data.

    python scripts/build_graph.py --raw /scratch/.../raw --state NC --out graph_NC
    python scripts/build_graph.py --raw /scratch/.../raw --state NC --huc4 0302 0303 \
        --storm 2018-09-13 2018-09-18 --out graph_NC_capefear_neuse

Reads only the download folders (see docs/knowledge_graph.md for every rule used) and writes

  <out>/nodes/<type>.parquet     one table per node type, column `id` = "<type>:<key>"
  <out>/edges/<type>.parquet     one table per edge type, columns `src`, `dst` + attributes
  <out>/tables/wq_results.parquet  water-quality results with standardised units
  <out>/graph_report.md          counts, checks and assumptions
  <out>/graph_meta.json          parameters, inputs and versions

Node types
  reach          V_R: NHDPlus HR flowlines (Q, velocity, travel time, order, HUC12,
                 floodplain share of its catchment, design storm and storm rainfall)
  pond           V_P: NHD waterbodies (lake/pond, reservoir; swamps only with --include-swamps)
  lagoon         V_P: CAFO waste lagoons (NC DEQ lagoon counts, or points with --lagoon-points)
  cafo           permitted animal facilities (NC DEQ)
  field          optional (--fields): crop field polygons
  wq_station     Water Quality Portal stations with results
  npdes_facility EPA ECHO NPDES facilities (inspections, violations, enforcement)

Edge types
  flows_to       reach -> reach: f_e (split share), Q_w (m3/s), tau_e (s)
  drains_to      cafo/field/pond/lagoon -> reach: weight = overlap area / source area
  pond_on_reach  pond -> reach: the reach (ArtificialPath) runs through the pond
  has_lagoon     cafo -> lagoon
  observes       wq_station -> reach: the station samples this reach (snapped)
  discharges_to  npdes_facility -> reach: facility point lies in the reach's catchment
  same_site      cafo -> npdes_facility: within 500 m of each other

Rainfall: --storm START END picks the storm s*; Stage IV daily totals over those days are
summed per catchment and at every CAFO/lagoon, and the wettest single day is compared with
the Atlas 14 25-yr 24-h depth (R_u).
"""

from __future__ import annotations

import argparse
import json
import warnings
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore", message=".*geographic CRS.*")

from kgdata.rasters import atlas14_grid, sample, stage4_day  # noqa: E402

AREA_CRS = 5070          # CONUS Albers, equal area: all areas and distances
FT3_TO_M3 = 0.0283168
FT_TO_M = 0.3048
NETWORK_FTYPES = ("StreamRiver", "ArtificialPath", "CanalDitch", "Connector")
POND_FTYPES = {390: "LakePond", 436: "Reservoir"}
SWAMP_FTYPE = {466: "SwampMarsh"}
PERMIT_TYPES = {"NPD": "individual NPDES permit", "GPC": "general permit coverage",
                "NGP": "master general permit", "APR": "associated permit record", "UFT": "unpermitted facility"}
PERMIT_STATUS = {"EFF": "effective", "ADC": "administratively continued", "EXP": "expired", "TRM": "terminated"}
CAFO_SIC = ("0211", "0212", "0213", "0214", "0219", "0241", "0251", "0252", "0253", "0254", "0259")

# water-quality parameters and unit factors to a standard unit (lower-case, spaces removed)
WQ_GROUPS = {
    "Nitrate": "nitrate_n", "Inorganic nitrogen (nitrate and nitrite)": "nitrate_n",
    "Ammonia": "ammonia_n", "Ammonia and ammonium": "ammonia_n",
    "Escherichia coli": "ecoli",
}
WQ_STD_UNIT = {"nitrate_n": "mg/L as N", "ammonia_n": "mg/L as N", "ecoli": "count/100 mL"}
WQ_FACTORS = {
    "nitrate_n": {"mg/lasn": 1, "mg/lasno3": 14.007 / 62.004, "mg/lno3": 14.007 / 62.004,
                  "ueq/l": 0.014007, "mg/l": 1, "ppm": 1},
    "ammonia_n": {"mg/lasn": 1, "mg/lnh4": 14.007 / 18.038, "mg/lasnh4": 14.007 / 18.038,
                  "mg/lasnh3": 14.007 / 17.031, "ueq/l": 0.014007, "mg/l": 1, "ppm": 1},
    "ecoli": {"cfu/100ml": 1, "mpn/100ml": 1, "#/100ml": 1, "count/100ml": 1},
}
WQ_ASSUMED = {"mg/l", "ppm"}  # basis (as N vs as ion) not stated: assumed "as N"


def log(msg):
    print(msg, flush=True)


class Report:
    def __init__(self):
        self.sections, self.meta = [], {}

    def section(self, title):
        now = time.time()
        if self.sections:
            self.sections[-1].append(f"- _({now - self._t:.0f} s)_")
        self._t = now
        self.sections.append([f"## {title}", ""])
        log(f"[graph] {title}")

    def add(self, line):
        self.sections[-1].append(f"- {line}")

    def write(self, path, header):
        path.write_text("\n".join(header + sum(([*s, ""] for s in self.sections), [])))


def read_many(files, **kw):
    frames = [pd.read_parquet(f, **kw) for f in files if f.exists()]
    return pd.concat(frames, ignore_index=True) if frames else None


def read_many_geo(files, columns=None):
    frames = [gpd.read_parquet(f, columns=columns) for f in files if f.exists()]
    return pd.concat(frames, ignore_index=True) if frames else None


def lonlat(geoms):
    p = gpd.GeoSeries(geoms).to_crs(4326) if not isinstance(geoms, gpd.GeoSeries) else geoms.to_crs(4326)
    return p.x.round(6).values, p.y.round(6).values


def overlap_weights(src, catch, src_id="id"):
    """drains_to weights: area(src polygon ∩ catchment) / area(src polygon), equal-area CRS."""
    j = gpd.sjoin(src[[src_id, "geometry"]], catch[["NHDPlusID", "geometry"]], predicate="intersects")
    if j.empty:
        return pd.DataFrame(columns=["src", "catchment", "weight"])
    a = src.geometry.loc[j.index].values
    b = catch.geometry.loc[j["index_right"]].values
    import shapely
    inter = shapely.area(shapely.intersection(a, b))
    out = pd.DataFrame({"src": j[src_id].values, "catchment": j["NHDPlusID"].values,
                        "weight": inter / shapely.area(a)})
    return out[out["weight"] > 1e-4]


def point_catchment(points, catch, id_col="id"):
    j = gpd.sjoin(points[[id_col, "geometry"]], catch[["NHDPlusID", "geometry"]], predicate="within", how="left")
    j = j[~j.index.duplicated()]
    return j["NHDPlusID"].reindex(points.index)


# ----------------------------------------------------------------------------- study area

def study_huc4s(raw, st, wanted, rep):
    rep.section("Study area")
    hu8 = []
    for f in sorted((raw / "wbd").glob("*/WBDHU8.parquet")):
        g = gpd.read_parquet(f, columns=["huc8", "states", "geometry"])
        hu8.append(g[g["states"].fillna("").str.split(",").map(lambda x: st in x)])
    hu8 = pd.concat(hu8) if hu8 else None
    state_hu4 = sorted({h[:4] for h in hu8["huc8"]}) if hu8 is not None and len(hu8) else []
    hu4 = wanted or state_hu4
    have = [h for h in hu4 if (raw / "nhdplus_hr" / h / "NHDFlowline.parquet").exists()]
    rep.add(f"HUC4s listing {st} in WBD: {', '.join(state_hu4) or 'none (no WBD?)'}")
    rep.add(f"HUC4s used: {', '.join(have)}" + (f"; not on disk: {', '.join(sorted(set(hu4) - set(have)))}"
                                                if set(hu4) - set(have) else ""))
    rep.add("whole HUC4s are used (not clipped at the state line) so the river network stays connected")
    if not have:
        raise SystemExit("no NHDPlus HR HUC4 folder found for the study area")
    return have


# ----------------------------------------------------------------------------- reaches

def build_reaches(raw, hu4s, rep):
    rep.section("Reaches (V_R) and flows_to")
    root = raw / "nhdplus_hr"
    fl = read_many_geo([root / h / "NHDFlowline.parquet" for h in hu4s])
    fl = fl.drop_duplicates("NHDPlusID")
    vaa = read_many([root / h / "NHDPlusFlowlineVAA.parquet" for h in hu4s]).drop_duplicates("NHDPlusID")
    erom = read_many([root / h / "NHDPlusEROMMA.parquet" for h in hu4s]).drop_duplicates("NHDPlusID")
    flow = read_many([root / h / "NHDPlusFlow.parquet" for h in hu4s]).drop_duplicates(["FromNHDPID", "ToNHDPID"])
    fl = fl[fl.get("ftype_name", pd.Series("", index=fl.index)) != "Coastline"]
    r = fl.merge(vaa[["NHDPlusID", "Divergence", "HydroSeq", "StreamOrde"]], on="NHDPlusID", how="left") \
          .merge(erom[["NHDPlusID", "QAMA", "VAMA"]], on="NHDPlusID", how="left")
    r["in_network"] = r["HydroSeq"].notna()
    r["q_m3s"] = r["QAMA"] * FT3_TO_M3
    r["v_ms"] = r["VAMA"] * FT_TO_M
    r["v_source"] = np.where(r["v_ms"] > 0, "EROM", None)
    # fallback velocity: median EROM velocity of reaches with the same stream order
    good = r[r["v_ms"] > 0]
    by_order = good.groupby("StreamOrde")["v_ms"].median()
    overall = float(good["v_ms"].median()) if len(good) else 0.3
    miss = ~(r["v_ms"] > 0)
    fill = r.loc[miss, "StreamOrde"].map(by_order).fillna(overall)
    r.loc[miss, "v_ms"] = fill
    r.loc[miss, "v_source"] = np.where(r.loc[miss, "StreamOrde"].map(by_order).notna(), "order median", "overall median")
    r["tau_s"] = r["LengthKM"] * 1000 / r["v_ms"]
    rep.add(f"{len(r):,} reaches ({int(r['in_network'].sum()):,} in the flow network; coastlines dropped)")
    rep.add(f"types:{_tbl(r['ftype_name'].value_counts().to_dict())}")
    rep.add(f"velocity from EROM for {int((r['v_source'] == 'EROM').sum()):,}; filled with the median velocity of "
            f"the same stream order for {int((r['v_source'] == 'order median').sum()):,}, with the overall "
            f"median ({overall:.3f} m/s) for {int((r['v_source'] == 'overall median').sum()):,}")
    rep.add(f"discharge Q (QAMA) missing for {int(r['QAMA'].isna().sum()):,} reaches")

    # flows_to edges
    ids = set(r["NHDPlusID"])
    f = flow[(flow["FromNHDPID"] != 0) & flow["FromNHDPID"].isin(ids)]
    outlets = set(f.loc[f["ToNHDPID"] == 0, "FromNHDPID"])
    leaving = set(f.loc[(f["ToNHDPID"] != 0) & ~f["ToNHDPID"].isin(ids), "FromNHDPID"])
    e = f[f["ToNHDPID"].isin(ids)].rename(columns={"FromNHDPID": "u", "ToNHDPID": "v"})[["u", "v"]].copy()
    q = r.set_index("NHDPlusID")["QAMA"]
    e["q_down"] = e["v"].map(q).fillna(0).clip(lower=0)
    n_out = e.groupby("u")["v"].transform("size")
    tot = e.groupby("u")["q_down"].transform("sum")
    e["f_e"] = np.where(n_out == 1, 1.0, np.where(tot > 0, e["q_down"] / tot.replace(0, np.nan), 1.0 / n_out))
    e["f_method"] = np.where(n_out == 1, "single", np.where(tot > 0, "QAMA share", "equal split"))
    rr = r.set_index("NHDPlusID")
    e["Q_w_m3s"] = e["u"].map(rr["q_m3s"]) * e["f_e"]
    e["tau_e_s"] = e["u"].map(rr["tau_s"])
    hs_u, hs_v = e["u"].map(rr["HydroSeq"]), e["v"].map(rr["HydroSeq"])
    e["hydroseq_ok"] = hs_u > hs_v
    nsplit = int(e.loc[n_out > 1, "u"].nunique())
    rep.add(f"{len(e):,} flows_to edges; {nsplit:,} reaches split into 2+ branches "
            f"(f_e: QAMA share {int((e['f_method'] == 'QAMA share').sum()):,} edges, equal split "
            f"{int((e['f_method'] == 'equal split').sum()):,}); NHDPlus HR DivFrac is not populated, so it is not used")
    sums = e.groupby("u")["f_e"].sum()
    rep.add(f"f_e sums to 1 for {int((sums.sub(1).abs() < 1e-6).sum()):,} of {len(sums):,} upstream reaches")
    bad = int((~e["hydroseq_ok"]).sum())
    rep.add(f"acyclicity check (HydroSeq decreases downstream): {len(e) - bad:,} edges OK, {bad:,} not")
    rep.add(f"{len(outlets):,} outlet reaches (flow leaves the network); {len(leaving):,} reaches flow out of the "
            f"study HUC4s")
    r["is_outlet"] = r["NHDPlusID"].isin(outlets)
    r["leaves_study_area"] = r["NHDPlusID"].isin(leaving)
    edges = pd.DataFrame({"src": "reach:" + e["u"].astype(str), "dst": "reach:" + e["v"].astype(str),
                          "f_e": e["f_e"].round(6), "f_method": e["f_method"], "Q_w_m3s": e["Q_w_m3s"],
                          "tau_e_s": e["tau_e_s"], "hydroseq_ok": e["hydroseq_ok"]})
    rep.meta["reaches"] = len(r)
    return r, edges


def load_catchments(raw, hu4s, reach_ids, rep):
    root = raw / "nhdplus_hr"
    c = read_many_geo([root / h / "NHDPlusCatchment.parquet" for h in hu4s], columns=["NHDPlusID", "AreaSqKm", "geometry"])
    c = c.drop_duplicates("NHDPlusID").to_crs(AREA_CRS).reset_index(drop=True)
    c["has_reach"] = c["NHDPlusID"].isin(reach_ids)
    rep.add(f"{len(c):,} catchments; {int((~c['has_reach']).sum()):,} have no flowline (sinks / isolated areas): "
            "drains_to weight that falls there is not attached to a reach")
    return c


# ----------------------------------------------------------------------------- ponds

def build_ponds(raw, hu4s, reaches, catch, include_swamps, rep):
    rep.section("Ponds (V_P from NHD waterbodies)")
    root = raw / "nhdplus_hr"
    wb = read_many_geo([root / h / "NHDWaterbody.parquet" for h in hu4s]).drop_duplicates("Permanent_Identifier")
    keep = dict(POND_FTYPES, **(SWAMP_FTYPE if include_swamps else {}))
    p = wb[wb["FType"].isin(keep)].to_crs(AREA_CRS).reset_index(drop=True)
    p["id"] = "pond:" + p["Permanent_Identifier"].astype(str)
    rep.add(f"{len(p):,} ponds of types {', '.join(keep.values())} (of {len(wb):,} NHD waterbodies)"
            + ("" if include_swamps else "; swamps/marshes left out: in NC they often no longer match the land "
               "(see docs/knowledge_graph.md)"))
    # pond_on_reach via WBArea_Permanent_Identifier of artificial paths
    link = reaches[reaches["WBArea_Permanent_Identifier"].notna()][["NHDPlusID", "WBArea_Permanent_Identifier"]]
    link = link[link["WBArea_Permanent_Identifier"].isin(set(p["Permanent_Identifier"]))]
    on = pd.DataFrame({"src": "pond:" + link["WBArea_Permanent_Identifier"].astype(str),
                       "dst": "reach:" + link["NHDPlusID"].astype(str)})
    p["on_network"] = p["Permanent_Identifier"].isin(set(link["WBArea_Permanent_Identifier"]))
    rep.add(f"{int(p['on_network'].sum()):,} ponds have a reach running through them (pond_on_reach: {len(on):,} edges); "
            f"{int((~p['on_network']).sum()):,} are off the network and connect only through drains_to")
    w = overlap_weights(p, catch[catch["has_reach"]])
    d = pd.DataFrame({"src": w["src"], "dst": "reach:" + w["catchment"].astype(str), "weight": w["weight"].round(6),
                      "src_type": "pond"})
    pt = p.geometry.representative_point()
    x, y = lonlat(gpd.GeoSeries(pt, crs=AREA_CRS))
    nodes = pd.DataFrame({"id": p["id"], "permanent_identifier": p["Permanent_Identifier"],
                          "nhdplusid": p["NHDPlusID"], "ftype": p["FType"].map(keep), "fcode": p["FCode"],
                          "sewage_or_treatment_pond": p["FCode"].isin([43612, 43624]),
                          "area_m2": (p["AreaSqKm"] * 1e6).round(0), "on_network": p["on_network"],
                          "fdate": p.get("FDate"), "lon": x, "lat": y})
    return nodes, d, on


# ----------------------------------------------------------------------------- CAFOs / lagoons / fields

def build_cafos(raw, st, catch, radius_m, rep):
    rep.section("CAFOs and lagoons")
    f = raw / "nc_deq" / "animal_facilities.parquet"
    if st != "NC" or not f.exists():
        rep.add("no state CAFO list for this state in the builder yet (NC only)")
        return None
    g = gpd.read_parquet(f).to_crs(AREA_CRS).reset_index(drop=True)
    g["id"] = "cafo:" + g["permit_number"].astype(str)
    c_all = point_catchment(g, catch)
    rep.add(f"{len(g):,} CAFOs in the NC DEQ list; {int(c_all.isna().sum()):,} lie outside the study catchments "
            "and are left out")
    g = g[c_all.notna()].reset_index(drop=True)
    x, y = lonlat(g.geometry)
    nodes = pd.DataFrame({"id": g["id"], "permit_number": g["permit_number"], "name": g.get("facility_name"),
                          "animal": g.get("regulated_operation"), "activity": g.get("regulated_activity"),
                          "allowable_count": pd.to_numeric(g.get("allowable_count"), errors="coerce"),
                          "lagoons_reported": pd.to_numeric(g.get("number_of_lagoons"), errors="coerce").fillna(0).astype(int),
                          "permit_type": g.get("permit_type"), "county": g.get("county_name"), "lon": x, "lat": y})
    c_id = point_catchment(g, catch)
    nodes["catchment"] = c_id.astype("Int64").values
    poly = g[["id"]].copy()
    poly["geometry"] = g.geometry.buffer(radius_m) if radius_m > 0 else g.geometry.buffer(1)
    poly = gpd.GeoDataFrame(poly, geometry="geometry", crs=AREA_CRS)
    w = overlap_weights(poly, catch[catch["has_reach"]])
    rep.add(f"{len(nodes):,} CAFOs in the graph")
    rep.add(f"drains_to: the farm is approximated by a {radius_m:g} m circle around its point (no farm outlines "
            f"are published); {len(w):,} edges")
    wsum = w.groupby("src")["weight"].sum().reindex(nodes["id"]).fillna(0)
    rep.add(f"weight reaching a reach, per CAFO: {_stats(wsum)}; {int((wsum < 0.99).sum()):,} CAFOs lose "
            "part of the weight (circle crosses a sink catchment or the edge of the study area)")
    d = pd.DataFrame({"src": w["src"], "dst": "reach:" + w["catchment"].astype(str), "weight": w["weight"].round(6),
                      "src_type": "cafo"})
    return nodes, d, g


def build_lagoons(cafo_nodes, cafo_geo, catch, lagoon_points, rep):
    if cafo_nodes is None:
        return None, None, None, None
    rows = []
    if lagoon_points is not None:
        lp = gpd.read_parquet(lagoon_points) if str(lagoon_points).endswith(".parquet") else gpd.read_file(lagoon_points)
        lp = lp.to_crs(AREA_CRS).reset_index(drop=True)
        near = gpd.sjoin_nearest(lp[["geometry"]], cafo_geo[["id", "geometry"]], max_distance=500, distance_col="d", how="left")
        near = near[~near.index.duplicated()].reindex(lp.index)
        k = Counter()
        for i, (geom, cid, dist) in enumerate(zip(lp.geometry, near["id"], near["d"])):
            key = cid.split(":", 1)[1] if isinstance(cid, str) else "unmatched"
            k[key] += 1
            rows.append((f"lagoon:{key}:{k[key]}", cid if isinstance(cid, str) else None, geom, "lagoon point",
                         round(float(dist), 1) if pd.notna(dist) else None))
        rep.add(f"lagoons from --lagoon-points: {len(lp):,} points, {int(near['id'].notna().sum()):,} matched to a CAFO within 500 m")
    else:
        g = cafo_geo.set_index("id")
        for cid, n in zip(cafo_nodes["id"], cafo_nodes["lagoons_reported"]):
            for k in range(1, int(n) + 1):
                rows.append((f"lagoon:{cid.split(':', 1)[1]}:{k}", cid, g.geometry[cid], "CAFO point", None))
        rep.add(f"{len(rows):,} lagoon nodes from NC DEQ lagoon counts; placed at the CAFO point because no lagoon "
                "outlines/points are loaded (use --lagoon-points when available)")
    lg = gpd.GeoDataFrame(rows, columns=["id", "cafo", "geometry", "location_from", "dist_to_cafo_m"], crs=AREA_CRS)
    x, y = lonlat(lg.geometry)
    nodes = pd.DataFrame({"id": lg["id"], "cafo": lg["cafo"], "location_from": lg["location_from"],
                          "dist_to_cafo_m": lg["dist_to_cafo_m"], "lon": x, "lat": y})
    nodes["catchment"] = point_catchment(lg, catch).astype("Int64").values
    has = nodes.dropna(subset=["cafo"])
    has_lagoon = pd.DataFrame({"src": has["cafo"], "dst": has["id"]})
    ok = nodes["catchment"].notna() & nodes["catchment"].isin(set(catch.loc[catch["has_reach"], "NHDPlusID"]))
    d = pd.DataFrame({"src": nodes.loc[ok, "id"], "dst": "reach:" + nodes.loc[ok, "catchment"].astype(str),
                      "weight": 1.0, "src_type": "lagoon"})
    return nodes, d, has_lagoon, lg


def build_fields(path, id_col, catch, rep):
    rep.section("Fields")
    f = gpd.read_parquet(path) if str(path).endswith(".parquet") else gpd.read_file(path)
    f = f.to_crs(AREA_CRS).reset_index(drop=True)
    f["id"] = "field:" + f[id_col].astype(str)
    w = overlap_weights(f, catch[catch["has_reach"]])
    x, y = lonlat(f.geometry.representative_point())
    nodes = pd.DataFrame({"id": f["id"], "area_m2": f.geometry.area.round(0), "lon": x, "lat": y})
    for c in f.columns:
        if c not in ("id", "geometry") and c not in nodes:
            nodes[c] = f[c].values
    rep.add(f"{len(nodes):,} fields; {len(w):,} drains_to edges")
    return nodes, pd.DataFrame({"src": w["src"], "dst": "reach:" + w["catchment"].astype(str),
                                "weight": w["weight"].round(6), "src_type": "field"})


# ----------------------------------------------------------------------------- flood zones

def flood_zones(raw, st, catch, points, rep):
    """Share of each catchment inside the SFHA, and the flood zone at each point (CAFOs, lagoons).

    The share is counted on a grid (30 m, coarser only for very large study areas): exact
    polygon intersections of every catchment with every floodplain polygon took ~8 minutes
    for one HUC4. Points are matched to the exact polygons.
    """
    import pyarrow.parquet as pq
    import shapely
    from rasterio import features
    from rasterio.transform import from_origin

    rep.section("Flood zones (FEMA NFHL)")
    f = raw / "nfhl" / st / "flood_hazard_zones.parquet"
    if not f.exists():
        rep.add(f"`nfhl/{st}/flood_hazard_zones.parquet` not found: no flood attributes")
        return None, None
    pf = pq.ParquetFile(f)
    crs = json.loads(pf.schema_arrow.metadata[b"geo"])["columns"]["geometry"].get("crs")
    minx, miny, maxx, maxy = catch.total_bounds
    res = max(30.0, float(np.ceil(np.sqrt((maxx - minx) * (maxy - miny) / 150e6))))
    shape = (int(np.ceil((maxy - miny) / res)), int(np.ceil((maxx - minx) / res)))
    tr = from_origin(minx, maxy, res, res)
    cidx = features.rasterize(((g, i + 1) for i, g in enumerate(catch.geometry)), out_shape=shape,
                              transform=tr, fill=0, dtype="int32")
    mask = np.zeros(shape, dtype=bool)
    n_all = np.bincount(cidx.ravel(), minlength=len(catch) + 1)[1:]
    small = catch[n_all < 4]                 # too few cells: exact intersection instead
    small_area = pd.Series(0.0, index=small.index)
    cb = gpd.GeoSeries([shapely.box(minx, miny, maxx, maxy)], crs=AREA_CRS).to_crs(crs).total_bounds
    pz = []
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i, columns=["FLD_ZONE", "SFHA_TF", "geometry"]).to_pandas()
        z = gpd.GeoDataFrame(t.drop(columns="geometry"), geometry=shapely.from_wkb(t["geometry"].values), crs=crs)
        del t
        b = z.geometry.bounds
        z = z[(b.minx <= cb[2]) & (b.maxx >= cb[0]) & (b.miny <= cb[3]) & (b.maxy >= cb[1])]
        if z.empty:
            continue
        z = z.to_crs(AREA_CRS)
        if points is not None and len(points):
            j = gpd.sjoin(points[["id", "geometry"]], z, predicate="within")
            pz.append(pd.DataFrame(j[["id", "FLD_ZONE", "SFHA_TF"]]))
        s = z.geometry[z["SFHA_TF"] == "T"]
        if len(s):
            mask |= features.rasterize(((g, 1) for g in s), out_shape=shape, transform=tr, fill=0,
                                       dtype="uint8").astype(bool)
            if len(small):
                j = gpd.sjoin(small[["geometry"]], gpd.GeoDataFrame(geometry=s.values, crs=AREA_CRS),
                              predicate="intersects")
                if len(j):
                    a = shapely.area(shapely.intersection(small.geometry.loc[j.index].values,
                                                          s.values[j["index_right"].values]))
                    small_area = small_area.add(pd.Series(a, index=j.index).groupby(level=0).sum(), fill_value=0)
        log(f"    flood zones: row group {i + 1}/{pf.num_row_groups}")
    n_in = np.bincount(cidx[mask], minlength=len(catch) + 1)[1:]
    share = pd.Series(np.where(n_all > 0, n_in / np.maximum(n_all, 1), np.nan), index=catch.index)
    share.loc[small.index] = (small_area / small.geometry.area).clip(upper=1)
    rep.add(f"SFHA share counted on a {res:g} m grid for {len(catch):,} catchments (exact polygon intersection "
            f"for the {len(small):,} smaller than 4 cells); {int((share > 0).sum()):,} contain some SFHA "
            f"(median share among those {share[share > 0].median():.2f})")
    zones = None
    if pz:
        zones = pd.concat(pz).sort_values("SFHA_TF", ascending=False).drop_duplicates("id").set_index("id")
    return share, zones


# ----------------------------------------------------------------------------- rainfall

def rainfall(raw, start, end, bbox4326, rep):
    rep.section("Rainfall: storm s* (Stage IV) and design storm R_u (Atlas 14)")
    a14 = atlas14_grid(raw, bbox4326)
    if a14:
        rep.add(f"Atlas 14 grids used: {', '.join(a14[2])}")
    else:
        rep.add("no Atlas 14 grid covers the study area: R_u is missing")
    days, total, maxday, src = [], None, None, Counter()
    d = start
    while start and d <= end:
        g = stage4_day(raw, d, bbox4326)
        if g is None:
            rep.add(f"Stage IV {d}: not on disk, skipped")
        else:
            grid, tr, s = g
            src[s] += 1
            days.append(str(d))
            total = grid.copy() if total is None else np.where(np.isnan(total), grid, total + np.nan_to_num(grid))
            maxday = grid.copy() if maxday is None else np.fmax(maxday, grid)
        d += timedelta(days=1)
    if start:
        rep.add(f"storm s* = {start} … {end}: {len(days)} days read ({dict(src)}); each day is the 24 h ending "
                "12:00 UTC on that date")
    storm = (total, maxday, tr) if total is not None else None
    return a14, storm, days


def sample_rain(xs, ys, a14, storm):
    out = {}
    if a14:
        out["r_u_in"] = np.round(sample(a14[0], a14[1], xs, ys), 2)
    if storm:
        out["storm_total_in"] = np.round(sample(storm[0], storm[2], xs, ys), 2)
        out["storm_max_1day_in"] = np.round(sample(storm[1], storm[2], xs, ys), 2)
        if a14:
            out["storm_over_r_u"] = np.round(out["storm_max_1day_in"] / out["r_u_in"], 3)
    return out


# ----------------------------------------------------------------------------- water quality

def _unit_key(u):
    return re.sub(r"\s+", "", str(u)).lower() if isinstance(u, str) else ""


def build_wq(raw, st, reaches, catch, bbox4326, snap_m, rep):
    rep.section("Water quality (WQP) stations and results")
    root = raw / "wqp" / st
    if not (root / "stations.csv").exists():
        rep.add(f"`wqp/{st}/` not found")
        return None, None, None
    s = pd.read_csv(root / "stations.csv", dtype=str, low_memory=False)
    s["lon"], s["lat"] = pd.to_numeric(s["LongitudeMeasure"], errors="coerce"), pd.to_numeric(s["LatitudeMeasure"], errors="coerce")
    w, so, e, n = bbox4326
    s = s[s["lon"].between(w, e) & s["lat"].between(so, n)].reset_index(drop=True)
    sg0 = gpd.GeoDataFrame(s.assign(id=s["MonitoringLocationIdentifier"]),
                           geometry=gpd.points_from_xy(s["lon"], s["lat"]), crs=4326).to_crs(AREA_CRS)
    inside = point_catchment(sg0, catch).notna()
    s = s[inside.values].reset_index(drop=True)
    cols = ["MonitoringLocationIdentifier", "ActivityStartDate", "CharacteristicName", "ResultMeasureValue",
            "ResultMeasure/MeasureUnitCode", "ResultSampleFractionText", "ResultDetectionConditionText"]
    res = []
    for f in sorted(root.glob("results_*.csv")):
        hdr = pd.read_csv(f, nrows=0).columns
        r = pd.read_csv(f, dtype=str, usecols=[c for c in cols if c in hdr], low_memory=False)
        res.append(r[r["MonitoringLocationIdentifier"].isin(set(s["MonitoringLocationIdentifier"]))])
    res = pd.concat(res, ignore_index=True) if res else pd.DataFrame(columns=cols)
    res["param"] = res["CharacteristicName"].map(WQ_GROUPS)
    res["value"] = pd.to_numeric(res["ResultMeasureValue"], errors="coerce")
    uk = res["ResultMeasure/MeasureUnitCode"].map(_unit_key)
    fac = [WQ_FACTORS.get(p, {}).get(u, np.nan) if isinstance(p, str) else np.nan for p, u in zip(res["param"], uk)]
    res["value_std"] = res["value"] * np.array(fac, dtype=float)
    res["unit_std"] = res["param"].map(WQ_STD_UNIT)
    res["unit_assumed"] = uk.isin(WQ_ASSUMED) & res["param"].isin(["nitrate_n", "ammonia_n"])
    res["date"] = pd.to_datetime(res["ActivityStartDate"], errors="coerce")
    res = res.rename(columns={"MonitoringLocationIdentifier": "station", "ResultMeasure/MeasureUnitCode": "unit",
                              "ResultSampleFractionText": "fraction", "ResultDetectionConditionText": "detection"})
    unconv = res["value"].notna() & res["value_std"].isna()
    rep.add(f"{len(res):,} results at {res['station'].nunique():,} stations in the study catchments; "
            f"standardised for {int(res['value_std'].notna().sum()):,}; {int(res['value'].isna().sum()):,} without a value "
            f"(mostly non-detects); {int(unconv.sum()):,} with a unit not converted"
            + (":" + _tbl(res.loc[unconv, 'unit'].fillna('(none)').value_counts().head(6).to_dict()) if unconv.any() else ""))
    rep.add(f"{int(res['unit_assumed'].sum()):,} nitrogen results report plain mg/L; taken as 'as N' (flag unit_assumed)")
    s = s[s["MonitoringLocationIdentifier"].isin(set(res["station"]))]
    sg = gpd.GeoDataFrame(s, geometry=gpd.points_from_xy(s["lon"], s["lat"]), crs=4326).to_crs(AREA_CRS)
    net = reaches[reaches["ftype_name"].isin(NETWORK_FTYPES) & reaches["in_network"]][["NHDPlusID", "geometry"]].to_crs(AREA_CRS)
    j = gpd.sjoin_nearest(sg[["MonitoringLocationIdentifier", "geometry"]], net, max_distance=snap_m, distance_col="d", how="left")
    j = j[~j.index.duplicated()].reindex(sg.index)
    sg["reach"] = j["NHDPlusID"].astype("Int64").values
    sg["snap_m"] = j["d"].round(1).values
    agg = res.groupby(["station", "param"]).agg(n=("value", "size"), median=("value_std", "median")).unstack("param")
    nodes = pd.DataFrame({"id": "wq_station:" + sg["MonitoringLocationIdentifier"], "station": sg["MonitoringLocationIdentifier"],
                          "name": sg.get("MonitoringLocationName"), "org": sg.get("OrganizationIdentifier"),
                          "type": sg.get("MonitoringLocationTypeName"), "lon": sg["lon"].values, "lat": sg["lat"].values,
                          "snap_m": sg["snap_m"].values})
    for p in sorted(res["param"].dropna().unique()):
        nodes[f"n_{p}"] = nodes["station"].map(agg[("n", p)]).fillna(0).astype(int)
        nodes[f"median_{p}"] = nodes["station"].map(agg[("median", p)])
    dr = res.groupby("station")["date"].agg(["min", "max"])
    nodes["first_date"], nodes["last_date"] = nodes["station"].map(dr["min"]), nodes["station"].map(dr["max"])
    ok = sg["reach"].notna()
    obs = pd.DataFrame({"src": nodes.loc[ok.values, "id"].values, "dst": "reach:" + sg.loc[ok, "reach"].astype(str).values,
                        "snap_m": sg.loc[ok, "snap_m"].values})
    far = nodes.loc[nodes["snap_m"].isna(), "type"].fillna("?").value_counts().to_dict()
    rep.add(f"{len(nodes):,} stations inside the study catchments; {len(obs):,} snapped to a network reach within "
            f"{snap_m:g} m (observes); {len(nodes) - len(obs):,} farther away (lakes, estuaries, wells, or "
            f"coordinates off the stream){':' + _tbl(far) if far else ''}")
    keep = ["station", "date", "CharacteristicName", "param", "value", "unit", "fraction", "detection",
            "value_std", "unit_std", "unit_assumed"]
    return nodes, obs, res[[c for c in keep if c in res]]


# ----------------------------------------------------------------------------- ECHO

def build_npdes(raw, st, catch, cafo_geo, rep):
    rep.section("NPDES facilities (EPA ECHO)")
    root = raw / "echo" / st
    if not (root / "ICIS_FACILITIES.parquet").exists():
        rep.add(f"`echo/{st}/` not found")
        return None, None, None
    fac = pd.read_parquet(root / "ICIS_FACILITIES.parquet")
    fac["lon"], fac["lat"] = pd.to_numeric(fac["GEOCODE_LONGITUDE"], errors="coerce"), pd.to_numeric(fac["GEOCODE_LATITUDE"], errors="coerce")
    fac = fac.dropna(subset=["lon", "lat"]).drop_duplicates("NPDES_ID")
    g = gpd.GeoDataFrame(fac, geometry=gpd.points_from_xy(fac["lon"], fac["lat"]), crs=4326).to_crs(AREA_CRS)
    g["id"] = "npdes_facility:" + g["NPDES_ID"]
    g["catchment"] = point_catchment(g, catch).values
    g = g[g["catchment"].notna()].copy()
    caf = set()
    pc = root / "NPDES_PERM_COMPONENTS.parquet"
    if pc.exists():
        t = pd.read_parquet(pc)
        caf |= set(t.loc[t["COMPONENT_TYPE_CODE"].eq("CAF"), "EXTERNAL_PERMIT_NMBR"])
    sic = root / "NPDES_SICS.parquet"
    if sic.exists():
        t = pd.read_parquet(sic)
        caf |= set(t.loc[t["SIC_CODE"].isin(CAFO_SIC), "NPDES_ID"])

    ptype, pstatus = {}, {}
    pp = root / "ICIS_PERMITS.parquet"
    if pp.exists():
        t = pd.read_parquet(pp, columns=["EXTERNAL_PERMIT_NMBR", "VERSION_NMBR", "PERMIT_TYPE_CODE", "PERMIT_STATUS_CODE"])
        t["v"] = pd.to_numeric(t["VERSION_NMBR"], errors="coerce")
        t = t.sort_values("v").drop_duplicates("EXTERNAL_PERMIT_NMBR", keep="last").set_index("EXTERNAL_PERMIT_NMBR")
        ptype = t["PERMIT_TYPE_CODE"].map(PERMIT_TYPES).fillna(t["PERMIT_TYPE_CODE"])
        pstatus = t["PERMIT_STATUS_CODE"].map(PERMIT_STATUS).fillna(t["PERMIT_STATUS_CODE"])

    def count(table):
        p = root / f"{table}.parquet"
        return pd.read_parquet(p, columns=["NPDES_ID"])["NPDES_ID"].value_counts() if p.exists() else pd.Series(dtype=int)

    nodes = pd.DataFrame({"id": g["id"], "npdes_id": g["NPDES_ID"], "name": g.get("FACILITY_NAME"),
                          "facility_type": g.get("FACILITY_TYPE_CODE"), "city": g.get("CITY"),
                          "permit_type": g["NPDES_ID"].map(ptype), "permit_status": g["NPDES_ID"].map(pstatus),
                          "construction_stormwater": g["NPDES_ID"].str.upper().str.startswith("NCC"),
                          "cafo_like": g["NPDES_ID"].isin(caf), "lon": g["lon"].round(6), "lat": g["lat"].round(6)})
    nodes["inspections"] = nodes["npdes_id"].map(count("NPDES_INSPECTIONS")).fillna(0).astype(int)
    v = sum((count(t) for t in ("NPDES_PS_VIOLATIONS", "NPDES_CS_VIOLATIONS", "NPDES_SE_VIOLATIONS")), pd.Series(dtype=float))
    nodes["violations"] = nodes["npdes_id"].map(v).fillna(0).astype(int)
    nodes["formal_enforcements"] = nodes["npdes_id"].map(count("NPDES_FORMAL_ENFORCEMENT_ACTIONS")).fillna(0).astype(int)
    nodes["informal_enforcements"] = nodes["npdes_id"].map(count("NPDES_INFORMAL_ENFORCEMENT_ACTIONS")).fillna(0).astype(int)
    with_reach = g["catchment"].isin(set(catch.loc[catch["has_reach"], "NHDPlusID"]))
    dis = pd.DataFrame({"src": g.loc[with_reach, "id"], "dst": "reach:" + g.loc[with_reach, "catchment"].astype("int64").astype(str)})
    same = None
    if cafo_geo is not None:
        j = gpd.sjoin_nearest(cafo_geo[["id", "geometry"]], g[["id", "geometry"]], max_distance=500, distance_col="d",
                              lsuffix="cafo", rsuffix="npdes")
        same = pd.DataFrame({"src": j["id_cafo"], "dst": j["id_npdes"], "distance_m": j["d"].round(1)})
    rep.add(f"permit types:{_tbl(nodes['permit_type'].fillna('(no permit row)').value_counts().to_dict())}")
    rep.add(f"permit status:{_tbl(nodes['permit_status'].fillna('(no permit row)').value_counts().to_dict())}")
    rep.add(f"{int(nodes['construction_stormwater'].sum()):,} are construction-site stormwater coverages (NCC…); "
            "filter on `construction_stormwater` / `permit_status` for wastewater point sources")
    rep.add(f"{len(nodes):,} NPDES facilities with a point in the study catchments ({int(nodes['cafo_like'].sum()):,} CAFO-like); "
            f"discharges_to uses the facility point, not the outfall location")
    rep.add(f"same_site (CAFO within 500 m of an NPDES facility): {0 if same is None else len(same):,} edges")
    return nodes, dis, same


# ----------------------------------------------------------------------------- HUC12

def huc12_of(raw, hu4s, pts):
    files = sorted({raw / "wbd" / h[:2] / "WBDHU12.parquet" for h in hu4s})
    h = read_many_geo([f for f in files], columns=["huc12", "geometry"])
    if h is None:
        return pd.Series(index=pts.index, dtype=object)
    h = h.to_crs(AREA_CRS)
    j = gpd.sjoin(pts[["geometry"]], h, predicate="within", how="left")
    return j[~j.index.duplicated()]["huc12"].reindex(pts.index)


# ----------------------------------------------------------------------------- helpers

def _tbl(d):
    return "".join(f"\n  - {k}: {v:,}" for k, v in d.items())


def _stats(s):
    s = pd.Series(s).dropna()
    return "no values" if s.empty else f"min {s.min():.3f}, median {s.median():.3f}, max {s.max():.3f}"


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True, help="the download folder (`out` in configs/sources.yaml)")
    ap.add_argument("--state", default="NC")
    ap.add_argument("--huc4", nargs="+", help="HUC4s to include (default: every HUC4 WBD lists for the state)")
    ap.add_argument("--storm", nargs=2, metavar=("START", "END"), default=["2018-09-13", "2018-09-18"],
                    help="storm s* dates (YYYY-MM-DD), default Hurricane Florence")
    ap.add_argument("--cafo-radius-m", type=float, default=200, help="circle used as the farm area for drains_to")
    ap.add_argument("--snap-m", type=float, default=250, help="max distance from a WQ station to its reach")
    ap.add_argument("--include-swamps", action="store_true", help="also make pond nodes of NHD swamps/marshes")
    ap.add_argument("--lagoon-points", type=Path, help="lagoon point file (gpkg/geojson/parquet) to place lagoons")
    ap.add_argument("--fields", type=Path, help="field polygons (gpkg/geojson/parquet) for field nodes")
    ap.add_argument("--fields-id", default="id", help="id column of --fields")
    ap.add_argument("--out", type=Path, help="output folder (default graph_<ST>)")
    args = ap.parse_args()

    t0 = time.time()
    raw, st = args.raw, args.state.upper()
    out = args.out or Path(f"graph_{st}")
    (out / "nodes").mkdir(parents=True, exist_ok=True)
    (out / "edges").mkdir(parents=True, exist_ok=True)
    (out / "tables").mkdir(parents=True, exist_ok=True)
    rep = Report()
    nodes, edges = {}, {}

    hu4s = study_huc4s(raw, st, args.huc4, rep)
    reaches, edges["flows_to"] = build_reaches(raw, hu4s, rep)
    catch = load_catchments(raw, hu4s, set(reaches["NHDPlusID"]), rep)
    import shapely
    bbox4326 = tuple(gpd.GeoSeries([shapely.box(*catch.total_bounds)], crs=AREA_CRS).to_crs(4326).total_bounds)

    pond_nodes, pond_drain, edges["pond_on_reach"] = build_ponds(raw, hu4s, reaches, catch, args.include_swamps, rep)
    nodes["pond"] = pond_nodes
    drains = [pond_drain]

    cafo_geo = lg_geo = None
    r = build_cafos(raw, st, catch, args.cafo_radius_m, rep)
    if r:
        nodes["cafo"], d, cafo_geo = r
        drains.append(d)
        nodes["lagoon"], d, edges["has_lagoon"], lg_geo = build_lagoons(nodes["cafo"], cafo_geo, catch, args.lagoon_points, rep)
        drains.append(d)
    if args.fields:
        nodes["field"], d = build_fields(args.fields, args.fields_id, catch, rep)
        drains.append(d)
    edges["drains_to"] = pd.concat([d for d in drains if d is not None and len(d)], ignore_index=True)

    # flood zones at catchments and at CAFO / lagoon points
    pts = []
    if cafo_geo is not None:
        pts.append(cafo_geo[["id", "geometry"]])
    if lg_geo is not None:
        pts.append(lg_geo[["id", "geometry"]])
    pts = gpd.GeoDataFrame(pd.concat(pts, ignore_index=True), crs=AREA_CRS) if pts else None
    share, zones = flood_zones(raw, st, catch, pts, rep)
    for t in ("cafo", "lagoon"):
        if t in nodes and zones is not None:
            nodes[t]["flood_zone"] = nodes[t]["id"].map(zones["FLD_ZONE"])
            nodes[t]["in_sfha"] = nodes[t]["id"].map(zones["SFHA_TF"]).eq("T")
            rep.add(f"{t}s inside the SFHA: {int(nodes[t]['in_sfha'].sum()):,} of {len(nodes[t]):,}")

    # rainfall
    start, end = (date.fromisoformat(args.storm[0]), date.fromisoformat(args.storm[1])) if args.storm else (None, None)
    a14, storm, days = rainfall(raw, start, end, bbox4326, rep)
    for t in ("cafo", "lagoon"):
        if t in nodes:
            for k, v in sample_rain(nodes[t]["lon"].values, nodes[t]["lat"].values, a14, storm).items():
                nodes[t][k] = v
    if "cafo" in nodes and "storm_over_r_u" in nodes["cafo"]:
        x = nodes["cafo"]["storm_over_r_u"]
        rep.add(f"CAFOs where the wettest storm day exceeded R_u: {int((x > 1).sum()):,} of {int(x.notna().sum()):,}")

    # reach attributes from its catchment
    cpt = gpd.GeoDataFrame(catch[["NHDPlusID"]], geometry=catch.geometry.representative_point(), crs=AREA_CRS)
    cx, cy = lonlat(cpt.geometry)
    cat_attr = pd.DataFrame({"NHDPlusID": catch["NHDPlusID"], "catchment_km2": catch["AreaSqKm"],
                             "huc12": huc12_of(raw, hu4s, cpt).values})
    if share is not None:
        cat_attr["sfha_share"] = share.round(4).values
    for k, v in sample_rain(cx, cy, a14, storm).items():
        cat_attr[k] = v
    rx, ry = lonlat(reaches.geometry.interpolate(0.5, normalized=True))
    rn = reaches.drop(columns="geometry").merge(cat_attr, on="NHDPlusID", how="left")
    nodes["reach"] = pd.DataFrame({
        "id": "reach:" + rn["NHDPlusID"].astype(str), "nhdplusid": rn["NHDPlusID"], "ftype": rn.get("ftype_name"),
        "fcode": rn["FCode"], "length_km": rn["LengthKM"], "stream_order": rn["StreamOrde"],
        "hydroseq": rn["HydroSeq"], "divergence": rn["Divergence"], "q_m3s": rn["q_m3s"], "v_ms": rn["v_ms"],
        "v_source": rn["v_source"], "tau_s": rn["tau_s"], "in_network": rn["in_network"], "is_outlet": rn["is_outlet"],
        "leaves_study_area": rn["leaves_study_area"], "huc4": rn.get("huc4"),
        **{c: rn[c] for c in cat_attr.columns if c != "NHDPlusID"}, "lon": rx, "lat": ry})

    # water quality, NPDES
    wq_nodes, edges["observes"], wq_res = build_wq(raw, st, reaches, catch, bbox4326, args.snap_m, rep)
    if wq_nodes is not None:
        nodes["wq_station"] = wq_nodes
        wq_res.to_parquet(out / "tables" / "wq_results.parquet", index=False)
    np_nodes, edges["discharges_to"], edges["same_site"] = build_npdes(raw, st, catch, cafo_geo, rep)
    if np_nodes is not None:
        nodes["npdes_facility"] = np_nodes
    if "cafo" in nodes:
        cpts = gpd.GeoDataFrame(nodes["cafo"][["id"]], geometry=cafo_geo.geometry.values, crs=AREA_CRS)
        nodes["cafo"]["huc12"] = huc12_of(raw, hu4s, cpts).values

    # write
    rep.section("Graph totals")
    rep.sections[-1].append(f"- total build time {time.time() - t0:.0f} s")
    for k, v in nodes.items():
        v.to_parquet(out / "nodes" / f"{k}.parquet", index=False)
        rep.add(f"nodes {k}: {len(v):,}")
    for k, v in edges.items():
        if v is None:
            continue
        v.to_parquet(out / "edges" / f"{k}.parquet", index=False)
        rep.add(f"edges {k}: {len(v):,}")
    ids = set().union(*[set(v["id"]) for v in nodes.values()])
    dangling = {k: int((~v["src"].isin(ids) | ~v["dst"].isin(ids)).sum()) for k, v in edges.items() if v is not None}
    rep.add(f"edges whose src or dst is not a node: {dangling}")
    meta = {"state": st, "huc4": hu4s, "storm": args.storm, "storm_days_read": days,
            "cafo_radius_m": args.cafo_radius_m, "snap_m": args.snap_m, "include_swamps": args.include_swamps,
            "lagoon_points": str(args.lagoon_points) if args.lagoon_points else None,
            "fields": str(args.fields) if args.fields else None, "raw": str(raw),
            "area_crs": f"EPSG:{AREA_CRS}", "built": time.strftime("%Y-%m-%d %H:%M"),
            "nodes": {k: len(v) for k, v in nodes.items()},
            "edges": {k: (0 if v is None else len(v)) for k, v in edges.items()}}
    (out / "graph_meta.json").write_text(json.dumps(meta, indent=2, default=str))
    rep.write(out / "graph_report.md", [f"# Knowledge graph for {st}", "",
                                        f"Built {meta['built']} from `{raw}` in {time.time() - t0:.0f} s. "
                                        "Method and assumptions: docs/knowledge_graph.md", ""])
    log(f"\nwrote {out}/ ({time.time() - t0:.0f} s)")
    for k, v in meta["nodes"].items():
        log(f"  nodes {k:15s} {v:>9,}")
    for k, v in meta["edges"].items():
        log(f"  edges {k:15s} {v:>9,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
