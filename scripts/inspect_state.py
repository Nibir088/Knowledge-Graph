#!/usr/bin/env python3
"""Check everything downloaded for one state, for manual verification.

    python scripts/inspect_state.py --raw /scratch/.../raw --state NC
    python scripts/inspect_state.py --raw /scratch/.../raw --state NC --focus-huc4 0302 0303 --date 2018-09-16

Reads the download folders only (nothing is changed) and writes to --out (default inspect_<ST>/):

  report.md        one section per dataset: OK / PARTIAL / MISSING, counts, date ranges,
                   and what to look at by hand
  report.json      the same numbers, machine readable
  cafo_links.csv   one row per CAFO with what every other dataset says at that spot:
                   HUC12, NHDPlus catchment, nearest pond/lagoon and stream, Atlas 14
                   25-yr 24-h depth, Stage IV rainfall on --date, FEMA flood zone, nearest
                   water-quality station, nearest ECHO NPDES facility
  map.html         interactive map (open in a browser; needs internet for the basemap):
                   CAFOs with all the links above in their popup, flood zones, rainfall on
                   --date, Atlas 14 grid, streams and ponds, water-quality stations, ECHO
                   CAFO permits, HUC8 boundaries, satellite imagery to check lagoons

The state's area comes from WBD (`states` column), so watersheds that cross the border
are included. NHDPlus HR, flood zones and streams are mapped only for the focus HUC4s
(default: the two HUC4s with the most CAFOs) to keep the map light; the report covers
the whole state.
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import shutil
import sys
import tarfile
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# state CAFO lists: (label, file, id, name, animal, size, lagoons)
CAFO_LISTS = {
    "NC": [("NC DEQ", "nc_deq/animal_facilities.parquet", "permit_number", "facility_name",
            "regulated_operation", "allowable_count", "number_of_lagoons")],
    "IA": [("Iowa DNR", "ia_dnr/afo_facilities.parquet", "progid", "facName", "OperatType",
            "AnimalUnit", None)],
    "CA": [("Cal-FF", "ca_cafo/cal_ff_facilities.parquet", "facility_id", None, "animal_type",
            "footprint_sq_m", None),
           ("CIWQS", "ca_cafo/ciwqs_cafo_facilities.parquet", "facility_id", "facility_name",
            "cafo_type", "cafo_population", None)],
}
WATER_RIGHTS = {"WA": "wa_ecology", "CA": "ca_calwatrs"}
# NHD waterbody FCodes worth naming in popups
FCODES = {43612: "Sewage treatment pond", 43600: "Reservoir", 43601: "Aquaculture",
          43613: "Reservoir (nonearthen)", 43619: "Reservoir (nonearthen)",
          43624: "Treatment pond", 43604: "Tailings pond", 43605: "Tailings pond",
          43606: "Disposal pond", 43618: "Earthen reservoir", 43609: "Cooling pond",
          39000: "Lake/Pond", 39004: "Lake/Pond (perennial)", 39001: "Lake/Pond (intermittent)",
          39009: "Lake/Pond (perennial, average water level)", 39010: "Lake/Pond (perennial, normal pool)",
          39011: "Lake/Pond (perennial, at photo date)", 39012: "Lake/Pond (perennial, spillway level)",
          46600: "Swamp/Marsh", 46601: "Swamp/Marsh (intermittent)", 46602: "Swamp/Marsh (perennial)",
          49300: "Estuary"}
CAFO_SIC = ("0211", "0212", "0213", "0214", "0219", "0241", "0251", "0252", "0253", "0254", "0259")


# ----------------------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


class Section:
    def __init__(self, title):
        self.title, self.status, self.lines, self.checks, self.data = title, "OK", [], [], {}

    def add(self, line):
        self.lines.append(line)

    def worse(self, status):
        order = ["N/A", "OK", "PARTIAL", "MISSING"]
        if order.index(status) > order.index(self.status):
            self.status = status

    def md(self):
        out = [f"## {self.title}: **{self.status}**", ""]
        out += [f"- {x}" for x in self.lines]
        if self.checks:
            out += ["", "Check by hand:"] + [f"- [ ] {x}" for x in self.checks]
        return "\n".join(out) + "\n"


def table(counts, head=("value", "count"), limit=15):
    items = list(counts.items())[:limit]
    rows = [f"| {head[0]} | {head[1]} |", "|---|---:|"]
    rows += [f"| {k} | {v:,} |" if isinstance(v, (int, np.integer)) else f"| {k} | {v} |" for k, v in items]
    if len(counts) > limit:
        rows.append(f"| … {len(counts) - limit} more | |")
    return "\n  " + "\n  ".join(rows)


def stats(s):
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return "no values"
    return f"min {s.min():.2f}, median {s.median():.2f}, max {s.max():.2f} (n = {len(s):,})"


def read_points_csv(path, lat, lon, usecols=None):
    df = pd.read_csv(path, dtype=str, usecols=usecols, low_memory=False)
    x, y = pd.to_numeric(df[lon], errors="coerce"), pd.to_numeric(df[lat], errors="coerce")
    ok = x.notna() & y.notna()
    return gpd.GeoDataFrame(df[ok], geometry=gpd.points_from_xy(x[ok], y[ok]), crs=4326)


def to_grid(src_path, band, bbox, res, scale=1.0, valid=lambda a: np.isfinite(a)):
    """Resample one raster band onto a lon/lat grid over bbox. Returns (array, transform)."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    w, s, e, n = bbox
    width, height = int(np.ceil((e - w) / res)), int(np.ceil((n - s) / res))
    dst_t = from_origin(w, n, res, res)
    dst = np.full((height, width), np.nan, dtype="float32")
    with rasterio.open(src_path) as src:
        a = src.read(band).astype("float32")
        a[~valid(a)] = np.nan
        reproject(a, dst, src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                  dst_transform=dst_t, dst_crs="EPSG:4326", dst_nodata=np.nan,
                  resampling=Resampling.nearest)
    return dst * scale, dst_t


def sample(grid, transform, pts):
    from rasterio.transform import rowcol
    r, c = rowcol(transform, pts.x.values, pts.y.values)
    r, c = np.asarray(r), np.asarray(c)
    ok = (r >= 0) & (r < grid.shape[0]) & (c >= 0) & (c < grid.shape[1])
    out = np.full(len(pts), np.nan)
    out[ok] = grid[r[ok], c[ok]]
    return out


def colorize(a, vmax, ramp=((247, 251, 255), (107, 174, 214), (8, 48, 107), (255, 0, 128))):
    """Blue ramp for 0..vmax (magenta above), transparent where NaN or 0."""
    t = np.clip(np.nan_to_num(a / vmax, nan=0), 0, 1) * (len(ramp) - 2)
    i = np.minimum(t.astype(int), len(ramp) - 3)
    f = (t - i)[..., None]
    lo, hi = np.array(ramp)[i], np.array(ramp)[i + 1]
    rgb = lo + (hi - lo) * f
    rgb[np.nan_to_num(a) > vmax] = ramp[-1]
    alpha = np.where(np.isnan(a) | (a <= 0), 0, 200)
    return np.dstack([rgb, alpha]).astype("uint8")


def popup_html(row, fields):
    rows = "".join(f"<tr><th style='text-align:left;padding-right:8px'>{html.escape(k)}</th>"
                   f"<td>{html.escape(str(v))}</td></tr>" for k, v in fields if v not in (None, "", "nan"))
    return f"<table style='font-size:12px'>{rows}</table>"


def union(geoms):
    return geoms.union_all() if hasattr(geoms, "union_all") else geoms.unary_union  # geopandas < 1.0


def fmt(v, nd=2):
    try:
        if v is None or pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return f"{v:,.{nd}f}" if isinstance(v, (float, np.floating)) else str(v)


# ----------------------------------------------------------------------------- datasets

def state_area(raw, st, sec):
    """HUC8s / HUC12s whose WBD `states` list contains st."""
    files = sorted((raw / "wbd").glob("*/WBDHU8.parquet"))
    if 0 < len(files) < 22:
        sec.worse("PARTIAL")
        sec.add(f"WBD has only {len(files)} of 22 HUC2 regions ({', '.join(f.parent.name for f in files)}); "
                "parts of the state in other regions are not seen, and CAFOs there show up as "
                "'outside every HUC12'")
    if not files:
        sec.worse("MISSING")
        sec.add("no `wbd/<HU2>/WBDHU8.parquet` files; the state area cannot be found")
        return None, None
    hu8 = []
    for f in files:
        g = gpd.read_parquet(f, columns=["huc8", "name", "states", "areasqkm", "geometry"])
        hu8.append(g[g["states"].fillna("").str.split(",").map(lambda x: st in x)])
    hu8 = pd.concat(hu8).to_crs(4326)
    if hu8.empty:
        sec.worse("MISSING")
        sec.add(f"no HUC8 lists {st} in its `states` column")
        return None, None
    hu12 = []
    for hu2 in sorted({h[:2] for h in hu8["huc8"]}):
        f = raw / "wbd" / hu2 / "WBDHU12.parquet"
        if not f.exists():
            sec.worse("PARTIAL")
            sec.add(f"missing `{f.relative_to(raw)}`")
            continue
        g = gpd.read_parquet(f, columns=["huc12", "name", "states", "geometry"])
        hu12.append(g[g["states"].fillna("").str.split(",").map(lambda x: st in x)])
    hu12 = pd.concat(hu12).to_crs(4326) if hu12 else None
    hu4 = sorted({h[:4] for h in hu8["huc8"]})
    sec.add(f"{len(hu8):,} HUC8s and {0 if hu12 is None else len(hu12):,} HUC12s list {st} "
            f"(including ones that cross the state line)")
    sec.add(f"HUC2 regions: {', '.join(sorted({h[:2] for h in hu4}))}; HUC4s ({len(hu4)}): {', '.join(hu4)}")
    sec.add(f"state bounding box (from those HUC12s): {np.round((hu12 if hu12 is not None else hu8).total_bounds, 3).tolist()}")
    sec.checks.append("HUC8 outlines on the map follow the state's rivers and border region")
    sec.data.update(huc8=len(hu8), huc12=0 if hu12 is None else len(hu12), huc4=hu4)
    return hu8, hu12


def load_cafos(raw, st, sec):
    frames = []
    for label, rel, idc, namec, animc, sizec, lagc in CAFO_LISTS.get(st, []):
        f = raw / rel
        if not f.exists():
            sec.worse("PARTIAL")
            sec.add(f"{label}: `{rel}` not found")
            continue
        g = gpd.read_parquet(f).to_crs(4326)
        d = pd.DataFrame({
            "cafo_source": label,
            "cafo_id": g[idc].astype(str),
            "cafo_name": g[namec].astype(str) if namec else "",
            "animal": g[animc].astype(str) if animc else "",
            "size": pd.to_numeric(g[sizec], errors="coerce") if sizec else np.nan,
            "lagoons": pd.to_numeric(g[lagc], errors="coerce") if lagc else np.nan,
        })
        if "coord_ok" in g:
            d["coord_ok"] = g["coord_ok"].values
        geom = g.geometry.representative_point() if (g.geom_type != "Point").any() else g.geometry
        frames.append(gpd.GeoDataFrame(d, geometry=geom.values, crs=4326))
        sec.add(f"**{label}** (`{rel}`): {len(g):,} facilities")
        sec.add(f"{label} by type:{table(d['animal'].value_counts().to_dict(), ('type', 'facilities'))}")
        if lagc:
            sec.add(f"{label}: {int((d['lagoons'] > 0).sum()):,} facilities report at least one lagoon "
                    f"({int(d['lagoons'].sum()):,} lagoons in total)")
        if sizec:
            sec.add(f"{label} `{sizec}`: {stats(d['size'])}")
    if not frames:
        if st not in CAFO_LISTS:
            sec.add(f"no state CAFO list is collected for {st}; see the ECHO section for permitted CAFOs")
        sec.worse("MISSING" if st in CAFO_LISTS else "PARTIAL")
        return None
    cafo = pd.concat(frames, ignore_index=True)
    cafo = gpd.GeoDataFrame(cafo, geometry="geometry", crs=4326)
    sec.checks += ["open a few CAFOs on the satellite layer: are the barns and lagoons where the point is?",
                   "points far from any farm, or in the wrong county, are location errors in the source"]
    return cafo


def check_bounds(cafo, hu12, sec):
    if cafo is None or hu12 is None:
        return
    inside = gpd.sjoin(cafo[["geometry"]], hu12[["huc12", "geometry"]], predicate="within", how="left")
    inside = inside[~inside.index.duplicated()]
    cafo["huc12"] = inside["huc12"].values
    out = cafo["huc12"].isna()
    if out.any():
        sec.worse("PARTIAL")
        sec.add(f"{int(out.sum())} CAFO points fall outside every HUC12 of the state (wrong coordinates?): "
                f"{', '.join(cafo.loc[out, 'cafo_id'].head(10))}")
    else:
        sec.add("every CAFO point lies inside one of the state's HUC12s")


def nhdplus(raw, st, hu4_all, focus, cafo, utm, sec, layers_out, min_order=4):
    root = raw / "nhdplus_hr"
    if not root.exists():
        sec.worse("MISSING")
        sec.add("no `nhdplus_hr/` folder")
        return
    need = ["NHDFlowline", "NHDPlusFlow", "NHDPlusFlowlineVAA", "NHDPlusEROMMA", "NHDPlusCatchment",
            "NHDWaterbody", "NHDPlusSink", "NHDPlusDivFracMP"]
    missing, partial = [], []
    for h in hu4_all:
        have = [n for n in need if (root / h / f"{n}.parquet").exists()]
        if not have:
            missing.append(h)
        elif len(have) < len(need):
            partial.append(f"{h} ({len(have)}/8)")
    sec.add(f"HUC4s of the state: {len(hu4_all)}; complete: {len(hu4_all) - len(missing) - len(partial)}"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; incomplete: {', '.join(partial)}" if partial else ""))
    if missing or partial:
        sec.worse("PARTIAL")
    flows, wbs, cats = [], [], []
    for h in focus:
        if not (root / h / "NHDFlowline.parquet").exists():
            continue
        fl = gpd.read_parquet(root / h / "NHDFlowline.parquet").to_crs(4326)
        vaa = pd.read_parquet(root / h / "NHDPlusFlowlineVAA.parquet", columns=["NHDPlusID", "StreamOrde"])
        fl = fl.merge(vaa, on="NHDPlusID", how="left")
        wb = gpd.read_parquet(root / h / "NHDWaterbody.parquet").to_crs(4326)
        cat = gpd.read_parquet(root / h / "NHDPlusCatchment.parquet").to_crs(4326)
        flows.append(fl), wbs.append(wb), cats.append(cat)
        ft = fl["ftype_name"].value_counts().to_dict() if "ftype_name" in fl else {}
        wt = wb["FCode"].map(lambda c: FCODES.get(int(c), str(c)) if pd.notna(c) else "").value_counts().to_dict()
        sec.add(f"**{h}**: {len(fl):,} flowlines, {len(wb):,} waterbodies, {len(cat):,} catchments; "
                f"flowline types:{table(ft, ('FType', 'flowlines'), 8)}")
        sec.add(f"{h} waterbodies by FCode:{table(wt, ('FCode', 'waterbodies'), 10)}")
    if not flows:
        sec.add("none of the focus HUC4s is downloaded; no NHDPlus links for CAFOs")
        return
    fl = pd.concat(flows, ignore_index=True)
    wb = pd.concat(wbs, ignore_index=True)
    cat = pd.concat(cats, ignore_index=True)
    sec.data.update(focus_flowlines=len(fl), focus_waterbodies=len(wb))
    if cafo is not None:
        c = cafo.to_crs(utm)
        k = gpd.sjoin(c[["geometry"]], cat.to_crs(utm)[["NHDPlusID", "geometry"]], predicate="within", how="left")
        k = k[~k.index.duplicated()]
        cafo["catchment_id"] = k["NHDPlusID"].astype("Int64").values
        w = gpd.sjoin_nearest(c[["geometry"]], wb.to_crs(utm)[["NHDPlusID", "FCode", "AreaSqKm", "geometry"]],
                              how="left", max_distance=2000, distance_col="d")
        w = w[~w.index.duplicated()]
        cafo["pond_dist_m"] = w["d"].round(0).values
        cafo["pond_fcode"] = w["FCode"].map(lambda v: FCODES.get(int(v), str(int(v))) if pd.notna(v) else "").values
        cafo["pond_area_m2"] = (w["AreaSqKm"] * 1e6).round(0).values
        f = gpd.sjoin_nearest(c[["geometry"]], fl.to_crs(utm)[["NHDPlusID", "StreamOrde", "ftype_name", "geometry"]],
                              how="left", max_distance=5000, distance_col="d")
        f = f[~f.index.duplicated()]
        cafo["stream_dist_m"] = f["d"].round(0).values
        cafo["stream_id"] = f["NHDPlusID"].astype("Int64").values
        cafo["stream_order"] = f["StreamOrde"].values
        inf = cafo["catchment_id"].notna()
        sec.add(f"CAFOs inside the focus HUC4s' catchments: {int(inf.sum()):,} of {len(cafo):,}")
        near = cafo.loc[inf, "pond_dist_m"]
        sec.add(f"of those, {int((near <= 100).sum()):,} have an NHD waterbody within 100 m and "
                f"{int(near.isna().sum()):,} have none within 2 km; nearest waterbody types:"
                f"{table(cafo.loc[inf & (cafo['pond_dist_m'] <= 300), 'pond_fcode'].value_counts().to_dict(), ('FCode within 300 m', 'CAFOs'), 8)}")
        sec.add(f"distance to the nearest flowline (m): {stats(cafo.loc[inf, 'stream_dist_m'])}")
        sec.checks += ["lagoons: compare the satellite layer with the NHD waterbodies near CAFOs; "
                       "many lagoons are not in NHD, or are mapped as plain ponds",
                       "flowlines follow the visible streams and arrows of the main rivers point downstream"]
        # map layers: waterbodies within 1 km of CAFOs, streams of order >= 3
        buf = c.buffer(1000).to_crs(4326)
        near_wb = wb[wb.intersects(union(buf))]
        layers_out["ponds"] = near_wb
    layers_out["streams"] = fl[pd.to_numeric(fl["StreamOrde"], errors="coerce").fillna(0).astype(float) >= min_order]
    layers_out["min_order"] = min_order
    sec.data.update(cafo_with_catchment=int(cafo["catchment_id"].notna().sum()) if cafo is not None else 0)


def atlas14(raw, bbox, cafo, sec, layers_out):
    tifs = sorted((raw / "atlas14").glob("*_in.tif")) if (raw / "atlas14").exists() else []
    if not tifs:
        sec.worse("MISSING")
        sec.add("no Atlas 14 GeoTIFF (`atlas14/<region>_25yr24h_in.tif`); was rasterio missing during the download?")
        return
    import rasterio
    from rasterio.warp import transform_bounds
    use = []
    for t in tifs:
        with rasterio.open(t) as r:
            b = transform_bounds(r.crs, "EPSG:4326", *r.bounds)
        if b[0] < bbox[2] and b[2] > bbox[0] and b[1] < bbox[3] and b[3] > bbox[1]:
            use.append(t)
    sec.add(f"grids on disk: {', '.join(t.name for t in tifs)}; covering the state: "
            f"{', '.join(t.name for t in use) or 'none'}")
    if not use:
        sec.worse("MISSING")
        return
    grid = None
    for t in use:
        g, tr = to_grid(t, 1, bbox, 0.01)
        grid = g if grid is None else np.where(np.isnan(grid), g, grid)
    sec.add(f"depth over the state (in): {stats(pd.Series(grid.ravel()))}")
    if cafo is not None:
        cafo["atlas14_in"] = np.round(sample(grid, tr, cafo.geometry), 2)
        sec.add(f"at CAFOs (in): {stats(cafo['atlas14_in'])}")
        if cafo["atlas14_in"].isna().any():
            sec.worse("PARTIAL")
            sec.add(f"{int(cafo['atlas14_in'].isna().sum())} CAFOs get no value (outside the grid)")
    sec.checks.append("values look plausible (about 5–8 in for a 25-yr 24-h storm in the Southeast, higher at the coast)")
    layers_out["atlas14"] = (grid, tr, float(np.nanpercentile(grid, 99)), "Atlas 14 25-yr 24-h depth (in)")


def stage4_inventory(raw, sec):
    root = raw / "stage4"
    if not root.exists():
        sec.worse("MISSING")
        sec.add("no `stage4/` folder")
        return
    per_year = {}
    for day in root.glob("*/*"):
        if not re.fullmatch(r"\d{8}", day.name):
            continue
        y = day.name[:4]
        names = [p.name for p in day.iterdir()]
        e = per_year.setdefault(y, Counter())
        e["days"] += 1
        for a in ("conus", "ak", "pr"):
            e[f"tif_{a}"] += any(n.endswith(f"_{a}.tif") for n in names)
        e["tar_only"] += (not any(n.endswith(".tif") for n in names)) and any(n.endswith(".tar") for n in names)
        e["empty"] += not names
    rows = ["| year | days | conus tif | ak tif | pr tif | GRIB tar only | empty |", "|---|---:|---:|---:|---:|---:|---:|"]
    for y in sorted(per_year):
        e = per_year[y]
        rows.append(f"| {y} | {e['days']} | {e['tif_conus']} | {e['tif_ak']} | {e['tif_pr']} | {e['tar_only']} | {e['empty']} |")
    sec.add("days on disk per year:\n  " + "\n  ".join(rows))
    days = sorted(d.name for d in root.glob("*/*") if re.fullmatch(r"\d{8}", d.name))
    if days:
        d0, d1 = date.fromisoformat(f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:]}"), \
            date.fromisoformat(f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}")
        expect = (d1 - d0).days + 1
        gaps = expect - len(days)
        sec.add(f"{days[0]} … {days[-1]}: {len(days):,} of {expect:,} days present" + (f" ({gaps} missing)" if gaps else ""))
        if gaps:
            sec.worse("PARTIAL")
    sec.data["stage4_years"] = {y: dict(e) for y, e in per_year.items()}
    sec.checks.append("2016, 2019 and 2020 are GRIB-only on NOAA's server (expected); 2016 tars hold CONUS only")


def stage4_day(raw, day, area, bbox, cafo, sec, layers_out):
    folder = raw / "stage4" / f"{day:%Y}" / f"{day:%Y%m%d}"
    tif = folder / f"nws_precip_1day_{day:%Y%m%d}_{area}.tif"
    tar = folder / f"ncep_stage_iv_source_files_{day:%Y%m%d}.tar"
    tmp = None
    if tif.exists():
        grid, tr = to_grid(tif, 1, bbox, 0.02, valid=lambda a: np.isfinite(a) & (a >= 0))
        src = tif.name + " (band 1, observed, inches)"
    elif tar.exists():
        tmp = Path(tempfile.mkdtemp())
        with tarfile.open(tar) as tf:
            m = next((m for m in tf.getmembers() if m.name.endswith(f"{area}_{day:%Y%m%d}_24h.grb.gz")), None)
            if m is None:
                sec.add(f"{tar.name} has no {area} file")
                sec.worse("PARTIAL")
                return
            with tf.extractfile(m) as fh, open(tmp / "x.grb", "wb") as out:
                shutil.copyfileobj(gzip.GzipFile(fileobj=fh), out)
        grid, tr = to_grid(tmp / "x.grb", 1, bbox, 0.02, scale=1 / 25.4,
                           valid=lambda a: np.isfinite(a) & (a >= 0) & (a < 9999))
        src = f"{tar.name} → {m.name} (GRIB, mm converted to inches)"
    else:
        sec.worse("PARTIAL")
        sec.add(f"--date {day}: no file in `{folder.relative_to(raw)}`")
        return
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    sec.add(f"--date {day} (24 h ending 12:00 UTC) from {src}: over the state {stats(pd.Series(grid.ravel()))} in")
    if cafo is not None:
        col = f"rain_{day:%Y%m%d}_in"
        cafo[col] = np.round(sample(grid, tr, cafo.geometry), 2)
        sec.add(f"at CAFOs: {stats(cafo[col])} in; {int((cafo[col] >= 5).sum()):,} CAFOs got ≥ 5 in")
    sec.checks.append(f"the rainfall pattern on {day} matches what you know of that day (e.g. Florence's band over SE NC on 2018-09-15/16)")
    layers_out["rain"] = (grid, tr, max(float(np.nanpercentile(grid, 99.5)), 1.0), f"Stage IV rainfall {day} (in)")


def nfhl(raw, st, focus_bbox, cafo, utm, sec, layers_out):
    f = raw / "nfhl" / st / "flood_hazard_zones.parquet"
    if not f.exists():
        sec.worse("MISSING")
        sec.add(f"no `nfhl/{st}/flood_hazard_zones.parquet` (download unfinished, or no statewide file for {st})")
        return
    z = gpd.read_parquet(f)
    sec.add(f"{len(z):,} flood-zone polygons; CRS {z.crs.to_epsg() if z.crs else 'none'}")
    sec.add(f"by FLD_ZONE:{table(z['FLD_ZONE'].value_counts().to_dict(), ('zone', 'polygons'), 12)}")
    if "SFHA_TF" in z:
        sec.add(f"SFHA (1%-annual-chance floodplain, SFHA_TF = T): {int((z['SFHA_TF'] == 'T').sum()):,} polygons")
    if "DFIRM_ID" in z:
        sec.add(f"{z['DFIRM_ID'].nunique():,} DFIRM (county/community map) IDs")
    bad = (~z.geometry.is_valid).sum()
    if bad:
        sec.add(f"{int(bad):,} invalid polygons (repaired with make_valid for the joins)")
        z["geometry"] = z.geometry.make_valid()
    z = z.to_crs(4326)
    if cafo is not None:
        j = gpd.sjoin(cafo[["geometry"]], z[["FLD_ZONE", "SFHA_TF", "geometry"]], predicate="within", how="left")
        # a point can sit in overlapping polygons; prefer the SFHA one
        j = j.sort_values("SFHA_TF", ascending=False, na_position="last")
        j = j[~j.index.duplicated()].sort_index()
        cafo["flood_zone"] = j["FLD_ZONE"].values
        cafo["in_sfha"] = (j["SFHA_TF"] == "T").values
        sec.add(f"CAFO points inside the SFHA: {int(cafo['in_sfha'].sum()):,}; with no zone at all "
                f"(unmapped area): {int(cafo['flood_zone'].isna().sum()):,}")
        sec.add(f"CAFO points by zone:{table(cafo['flood_zone'].fillna('none').value_counts().to_dict(), ('zone', 'CAFOs'), 8)}")
    sec.checks += ["SFHA polygons line up with the streams and floodplains on the basemap",
                   "a CAFO point marks the farm, not its lagoon or fields; check whether the lagoon itself is in the zone"]
    w, s, e, n = focus_bbox
    zf = z.cx[w:e, s:n]
    zf = zf[zf["SFHA_TF"] == "T"] if "SFHA_TF" in zf else zf
    layers_out["sfha"] = zf


def wqp(raw, st, cafo, utm, sec, layers_out):
    root = raw / "wqp" / st
    if not (root / "stations.csv").exists():
        sec.worse("MISSING")
        sec.add(f"no `wqp/{st}/stations.csv`")
        return
    stn = read_points_csv(root / "stations.csv", "LatitudeMeasure", "LongitudeMeasure")
    sec.add(f"{len(stn):,} stations with coordinates; types:"
            f"{table(stn['MonitoringLocationTypeName'].value_counts().to_dict(), ('type', 'stations'), 8) if 'MonitoringLocationTypeName' in stn else ''}")
    files = sorted(root.glob("results_*.csv"))
    if not files:
        sec.worse("PARTIAL")
        sec.add("no results files")
        return
    cols = ["MonitoringLocationIdentifier", "ActivityStartDate", "CharacteristicName",
            "ResultMeasureValue", "ResultMeasure/MeasureUnitCode", "ResultSampleFractionText"]
    per = {}
    counts_station = Counter()
    units = Counter()
    dmin, dmax = None, None
    for f in files:
        y = f.stem.split("_")[-1]
        hdr = pd.read_csv(f, nrows=0).columns
        r = pd.read_csv(f, dtype=str, usecols=[c for c in cols if c in hdr], low_memory=False)
        per[y] = r["CharacteristicName"].value_counts().to_dict() if len(r) else {}
        counts_station.update(r["MonitoringLocationIdentifier"].dropna())
        if "ResultMeasure/MeasureUnitCode" in r:
            units.update((r["CharacteristicName"].fillna("") + " [" + r["ResultMeasure/MeasureUnitCode"].fillna("?") + "]"))
        if len(r):
            d = pd.to_datetime(r["ActivityStartDate"], errors="coerce")
            dmin = d.min() if dmin is None else min(dmin, d.min())
            dmax = d.max() if dmax is None else max(dmax, d.max())
    chars = sorted({c for v in per.values() for c in v})
    rows = ["| year | " + " | ".join(chars) + " |", "|---|" + "---:|" * len(chars)]
    rows += [f"| {y} | " + " | ".join(f"{per[y].get(c, 0):,}" for c in chars) + " |" for y in sorted(per)]
    sec.add("results per year and characteristic:\n  " + "\n  ".join(rows))
    sec.add(f"sample dates {dmin:%Y-%m-%d} … {dmax:%Y-%m-%d}" if dmin is not None and pd.notna(dmin)
            else "no dated samples")
    sec.add(f"units in use:{table(dict(units.most_common(12)), ('characteristic [unit]', 'results'), 12)}")
    stn["n_results"] = stn["MonitoringLocationIdentifier"].map(counts_station).fillna(0).astype(int)
    sampled = stn[stn["n_results"] > 0]
    sec.add(f"stations with at least one result: {len(sampled):,}")
    empty_years = [y for y in sorted(per) if not per[y]]
    if empty_years:
        sec.worse("PARTIAL")
        sec.add(f"years with an empty results file: {', '.join(empty_years)}")
    sec.checks += ["mixed units (mg/L as N vs mg/L as NO3, cfu vs MPN): convert before comparing values",
                   "stations near CAFOs are on the streams they drain to (not across a divide)"]
    if cafo is not None and len(sampled):
        n = gpd.sjoin_nearest(cafo.to_crs(utm)[["geometry"]],
                              sampled.to_crs(utm)[["MonitoringLocationIdentifier", "n_results", "geometry"]],
                              how="left", max_distance=20000, distance_col="d")
        n = n[~n.index.duplicated()]
        cafo["wq_station"] = n["MonitoringLocationIdentifier"].values
        cafo["wq_station_dist_m"] = n["d"].round(0).values
        cafo["wq_station_results"] = n["n_results"].values
        sec.add(f"distance from CAFOs to the nearest sampled station within 20 km (m): "
                f"{stats(cafo['wq_station_dist_m'])}; {int(cafo['wq_station'].isna().sum()):,} CAFOs have none within 20 km")
    layers_out["wq"] = sampled


def echo(raw, st, cafo, utm, sec, layers_out):
    root = raw / "echo" / st
    if not root.exists():
        sec.worse("MISSING")
        sec.add(f"no `echo/{st}/` folder")
        return
    import pyarrow.parquet as pq
    rows = {f.stem: pq.ParquetFile(f).metadata.num_rows for f in sorted(root.glob("*.parquet"))}
    if not rows:
        sec.worse("MISSING")
        sec.add(f"`echo/{st}/` has no tables (download or split unfinished?)")
        return
    sec.add(f"tables:{table(rows, ('table', 'rows'), 20)}")
    if "ICIS_FACILITIES" not in rows:
        sec.worse("PARTIAL")
        return
    fac = pd.read_parquet(root / "ICIS_FACILITIES.parquet")
    lat = next((c for c in ("GEOCODE_LATITUDE", "LATITUDE83", "LATITUDE") if c in fac), None)
    lon = next((c for c in ("GEOCODE_LONGITUDE", "LONGITUDE83", "LONGITUDE") if c in fac), None)
    caf_ids = set()
    if (root / "NPDES_PERM_COMPONENTS.parquet").exists():
        pc = pd.read_parquet(root / "NPDES_PERM_COMPONENTS.parquet")
        cc = next((c for c in pc.columns if "COMPONENT_TYPE_CODE" in c), None)
        if cc:
            sec.add(f"permit components:{table(pc[cc].value_counts().to_dict(), ('component', 'permits'), 12)}")
            idc = "EXTERNAL_PERMIT_NMBR" if "EXTERNAL_PERMIT_NMBR" in pc else pc.columns[0]
            caf_ids |= set(pc.loc[pc[cc].eq("CAF"), idc])
    if (root / "NPDES_SICS.parquet").exists():
        sic = pd.read_parquet(root / "NPDES_SICS.parquet")
        scol = next((c for c in sic.columns if c.startswith("SIC_CODE")), None)
        if scol:
            caf_ids |= set(sic.loc[sic[scol].isin(CAFO_SIC), "NPDES_ID"])
    sec.add(f"NPDES permits that look like CAFOs (component CAF, or livestock SIC 02xx): {len(caf_ids):,}")
    for t in ("NPDES_INSPECTIONS", "NPDES_PS_VIOLATIONS", "NPDES_CS_VIOLATIONS", "NPDES_SE_VIOLATIONS",
              "NPDES_FORMAL_ENFORCEMENT_ACTIONS", "NPDES_INFORMAL_ENFORCEMENT_ACTIONS"):
        if t in rows and caf_ids:
            d = pd.read_parquet(root / f"{t}.parquet", columns=["NPDES_ID"])
            sec.add(f"{t} rows for those CAFO permits: {int(d['NPDES_ID'].isin(caf_ids).sum()):,}")
    if lat and lon:
        g = fac.assign(_x=pd.to_numeric(fac[lon], errors="coerce"), _y=pd.to_numeric(fac[lat], errors="coerce"))
        g = g.dropna(subset=["_x", "_y"])
        g = gpd.GeoDataFrame(g, geometry=gpd.points_from_xy(g["_x"], g["_y"]), crs=4326)
        sec.add(f"facilities with coordinates: {len(g):,} of {len(fac):,} (`{lat}`, `{lon}`)")
        cg = g[g["NPDES_ID"].isin(caf_ids)]
        layers_out["echo_caf"] = cg
        if cafo is not None and len(g):
            n = gpd.sjoin_nearest(cafo.to_crs(utm)[["geometry"]],
                                  g.to_crs(utm)[["NPDES_ID", "FACILITY_NAME", "geometry"]] if "FACILITY_NAME" in g
                                  else g.to_crs(utm)[["NPDES_ID", "geometry"]],
                                  how="left", max_distance=500, distance_col="d")
            n = n[~n.index.duplicated()]
            cafo["echo_npdes_within_500m"] = n["NPDES_ID"].values
            sec.add(f"CAFOs with an ECHO NPDES facility within 500 m: {int(cafo['echo_npdes_within_500m'].notna().sum()):,} "
                    f"of {len(cafo):,}")
    else:
        sec.add("ICIS_FACILITIES has no coordinate columns")
    sec.checks.append("most state-permitted CAFOs (e.g. NC non-discharge permits) are NOT in ECHO; that is expected")


def other_state_files(raw, st, sec):
    found = False
    if st in WATER_RIGHTS and (raw / WATER_RIGHTS[st]).exists():
        for f in sorted((raw / WATER_RIGHTS[st]).glob("*.parquet")):
            import pyarrow.parquet as pq
            sec.add(f"`{f.relative_to(raw)}`: {pq.ParquetFile(f).metadata.num_rows:,} rows")
            found = True
    if not found:
        sec.add(f"no water-rights data is collected for {st}" if st not in WATER_RIGHTS
                else f"`{WATER_RIGHTS[st]}/` not found")
        if st in WATER_RIGHTS:
            sec.worse("MISSING")
        else:
            sec.status = "N/A"


# ----------------------------------------------------------------------------- map

def build_map(path, st, focus_bbox, cafo, hu8, layers):
    import folium
    from folium.raster_layers import ImageOverlay

    w, s, e, n = focus_bbox
    m = folium.Map(location=[(s + n) / 2, (w + e) / 2], zoom_start=9, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="Streets").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery", name="Satellite (check lagoons)").add_to(m)

    def raster(key, show):
        if key not in layers:
            return
        grid, tr, vmax, label = layers[key]
        h, wd = grid.shape
        bounds = [[tr.f - h * abs(tr.e), tr.c], [tr.f, tr.c + wd * tr.a]]
        ImageOverlay(colorize(grid, vmax), bounds=bounds, name=f"{label}, max colour {vmax:.1f}",
                     opacity=0.7, show=show, mercator_project=True).add_to(m)

    raster("rain", True)
    raster("atlas14", False)
    if hu8 is not None:
        h8 = hu8[["huc8", "name", "geometry"]].copy()
        h8["geometry"] = h8.geometry.simplify(0.002)
        folium.GeoJson(h8.to_json(), name="HUC8 boundaries",
                       style_function=lambda f: {"color": "#334155", "weight": 1.5, "fillOpacity": 0},
                       tooltip=folium.GeoJsonTooltip(["huc8", "name"])).add_to(m)
    if "sfha" in layers and len(layers["sfha"]):
        z = layers["sfha"][["FLD_ZONE", "geometry"]].copy()
        z["geometry"] = z.geometry.simplify(0.0002)
        folium.GeoJson(z.to_json(), name=f"FEMA SFHA, focus area ({len(z):,})", show=False,
                       style_function=lambda f: {"color": "#0ea5e9", "weight": 0.5, "fillColor": "#38bdf8", "fillOpacity": 0.35},
                       tooltip=folium.GeoJsonTooltip(["FLD_ZONE"])).add_to(m)
    if "streams" in layers and len(layers["streams"]):
        sl = layers["streams"][["NHDPlusID", "GNIS_Name", "StreamOrde", "ftype_name", "geometry"]
                               if "GNIS_Name" in layers["streams"] else ["NHDPlusID", "StreamOrde", "ftype_name", "geometry"]].copy()
        sl["NHDPlusID"] = sl["NHDPlusID"].astype(str)
        sl["StreamOrde"] = sl["StreamOrde"].astype(float)
        sl["geometry"] = sl.geometry.simplify(0.0003)
        folium.GeoJson(sl.to_json(), name=f"NHDPlus streams, order ≥ {layers.get('min_order', 4)} ({len(sl):,})",
                       style_function=lambda f: {"color": "#1d4ed8", "weight": 0.5 + 0.4 * (f["properties"]["StreamOrde"] or 1)},
                       tooltip=folium.GeoJsonTooltip([c for c in sl.columns if c != "geometry"])).add_to(m)
    if "ponds" in layers and len(layers["ponds"]):
        p = layers["ponds"].copy()
        p["type"] = p["FCode"].map(lambda c: FCODES.get(int(c), str(c)) if pd.notna(c) else "")
        p["NHDPlusID"] = p["NHDPlusID"].astype(str)
        p["area_m2"] = (p["AreaSqKm"] * 1e6).round(0)
        p = p[["NHDPlusID", "type", "FCode", "area_m2", "geometry"]]
        folium.GeoJson(p.to_json(), name=f"NHD waterbodies within 1 km of a CAFO ({len(p):,})",
                       style_function=lambda f: {"color": "#dc2626" if f["properties"]["FCode"] in (43612, 43624) else "#0891b2",
                                                 "weight": 1.5, "fillOpacity": 0.3},
                       tooltip=folium.GeoJsonTooltip(["type", "FCode", "area_m2", "NHDPlusID"])).add_to(m)
    if "wq" in layers and len(layers["wq"]):
        q = layers["wq"]
        grp = folium.FeatureGroup(name=f"WQP stations with results ({len(q):,})", show=False)
        for _, r in q.iterrows():
            folium.CircleMarker([r.geometry.y, r.geometry.x], radius=3, color="#16a34a", fill=True, weight=1,
                                tooltip=f"{r['MonitoringLocationIdentifier']}: {r['n_results']:,} results").add_to(grp)
        grp.add_to(m)
    if "echo_caf" in layers and len(layers["echo_caf"]):
        q = layers["echo_caf"]
        grp = folium.FeatureGroup(name=f"ECHO CAFO permits ({len(q):,})", show=False)
        for _, r in q.iterrows():
            folium.RegularPolygonMarker([r.geometry.y, r.geometry.x], number_of_sides=4, radius=5, color="#7c3aed",
                                        fill=True, tooltip=f"{r['NPDES_ID']} {r.get('FACILITY_NAME', '')}").add_to(grp)
        grp.add_to(m)
    if cafo is not None:
        grp = folium.FeatureGroup(name=f"CAFOs ({len(cafo):,})")
        skip = {"geometry"}
        for _, r in cafo.iterrows():
            fields = [(k, fmt(r[k])) for k in cafo.columns if k not in skip]
            color = "#dc2626" if bool(r.get("in_sfha", False)) else "#ea580c"
            folium.CircleMarker([r.geometry.y, r.geometry.x], radius=4, color=color, weight=1, fill=True,
                                fill_opacity=0.85, tooltip=f"{r['cafo_source']} {r['cafo_id']} {r['cafo_name']}",
                                popup=folium.Popup(popup_html(r, fields), max_width=420)).add_to(grp)
        grp.add_to(m)
    folium.Rectangle([[s, w], [n, e]], color="#111827", weight=1, dash_array="6", fill=False,
                     name="Focus area (NHDPlus, flood zones)").add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    legend = ("<div style='position:fixed;bottom:20px;left:20px;z-index:9999;background:white;padding:8px;"
              "border:1px solid #999;font-size:12px'><b>" + st + "</b><br>"
              "<span style='color:#ea580c'>●</span> CAFO &nbsp; <span style='color:#dc2626'>●</span> CAFO in SFHA<br>"
              "<span style='color:#dc2626'>▭</span> NHD sewage/treatment pond &nbsp; <span style='color:#0891b2'>▭</span> other waterbody<br>"
              "rain / Atlas 14: light → dark blue = 0 → max colour, magenta = above<br>"
              "click a CAFO for every linked value</div>")
    m.get_root().html.add_child(folium.Element(legend))
    m.save(str(path))


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True, help="the download folder (`out` in configs/sources.yaml)")
    ap.add_argument("--state", required=True, help="postal code, e.g. NC")
    ap.add_argument("--focus-huc4", nargs="+", help="HUC4s to map in detail (default: the 2 with most CAFOs)")
    ap.add_argument("--date", default="2018-09-16", help="Stage IV day to map and sample (YYYY-MM-DD)")
    ap.add_argument("--area", default="conus", help="Stage IV area: conus, ak or pr")
    ap.add_argument("--out", type=Path, help="output folder (default inspect_<ST>)")
    ap.add_argument("--min-order", type=int, default=4, help="smallest stream order drawn on the map")
    ap.add_argument("--no-map", action="store_true")
    args = ap.parse_args()

    raw, st = args.raw, args.state.upper()
    out = args.out or Path(f"inspect_{st}")
    out.mkdir(parents=True, exist_ok=True)
    day = date.fromisoformat(args.date)
    secs, layers = [], {}

    def section(title):
        s = Section(title)
        secs.append(s)
        log(f"[{st}] {title}")
        return s

    s_wbd = section("WBD watersheds (state area)")
    hu8, hu12 = state_area(raw, st, s_wbd)
    if hu8 is None:
        log("cannot continue without WBD")
        return 1
    bbox = tuple((hu12 if hu12 is not None else hu8).total_bounds)
    utm = hu8.estimate_utm_crs()

    s_cafo = section("State CAFO list")
    cafo = load_cafos(raw, st, s_cafo)
    check_bounds(cafo, hu12, s_cafo)

    focus = args.focus_huc4
    if not focus:
        if cafo is not None and "huc12" in cafo:
            focus = cafo["huc12"].dropna().str[:4].value_counts().index[:2].tolist()
        focus = focus or s_wbd.data["huc4"][:1]
    fb = hu8[hu8["huc8"].str[:4].isin(focus)]
    focus_bbox = tuple(fb.total_bounds) if len(fb) else bbox
    s_wbd.add(f"focus HUC4s for the map: {', '.join(focus)}")

    nhdplus(raw, st, s_wbd.data["huc4"], focus, cafo, utm, section(f"NHDPlus HR (focus {', '.join(focus)})"),
            layers, args.min_order)
    atlas14(raw, bbox, cafo, section("NOAA Atlas 14 (25-yr 24-h)"), layers)
    s4 = section("NOAA Stage IV rainfall")
    stage4_inventory(raw, s4)
    stage4_day(raw, day, args.area, bbox, cafo, s4, layers)
    nfhl(raw, st, focus_bbox, cafo, utm, section("FEMA NFHL flood zones"), layers)
    wqp(raw, st, cafo, utm, section("Water Quality Portal"), layers)
    echo(raw, st, cafo, utm, section("EPA ECHO (NPDES)"), layers)
    other_state_files(raw, st, section("Water rights"))

    md = [f"# What we have for {st}", "",
          f"Download folder: `{raw}`  ",
          f"State area from WBD: {s_wbd.data.get('huc8', 0)} HUC8s; focus HUC4s: {', '.join(focus)}; Stage IV day: {day}", "",
          "| dataset | status |", "|---|---|"]
    md += [f"| {x.title} | {x.status} |" for x in secs] + [""]
    md += [x.md() for x in secs]
    if cafo is not None:
        csv = out / "cafo_links.csv"
        c = cafo.copy()
        c["lon"], c["lat"] = c.geometry.x.round(6), c.geometry.y.round(6)
        pd.DataFrame(c.drop(columns="geometry")).to_csv(csv, index=False)
        md.append(f"Per-CAFO links: `{csv.name}` ({len(c):,} rows, columns: {', '.join(c.columns.drop('geometry'))})\n")
    (out / "report.md").write_text("\n".join(md))
    (out / "report.json").write_text(json.dumps(
        {x.title: {"status": x.status, "data": x.data, "notes": x.lines} for x in secs}, indent=2, default=str))
    if not args.no_map:
        build_map(out / "map.html", st, focus_bbox, cafo, hu8, layers)
    log(f"\nwrote {out / 'report.md'}" + ("" if args.no_map else f", {out / 'map.html'}")
        + (f", {out / 'cafo_links.csv'}" if cafo is not None else ""))
    for x in secs:
        log(f"  {x.status:8s} {x.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
