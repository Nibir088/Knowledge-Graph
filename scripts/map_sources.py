#!/usr/bin/env python3
"""Map the pollution source list (raw/sources/<ST>/source_year.parquet) for one year.

    python scripts/map_sources.py --raw /scratch/wyr6fx/knowledge-graph/raw --year 2024
    python scripts/map_sources.py --raw ... --year 2018 --size nitrogen_kg --states NC

Satellite basemap; for each state one layer per source type:
  colour  CAFO = orange, sewage treatment plant = blue, industrial facility = pink
  style   discharge measured = filled dot, size = --size (default total_waste_kg, log scale)
          discharge unknown  = hollow ring
          discharge none     = grey (stopped that year)
plus lines from measured plants/facilities to their discharge points (ECHO outfalls).
Click any source for its row of source_year.parquet.

Water network (NHDPlus HR, under the sources):
  --rivers-min-order N   whole states: streams of Strahler order >= N (default 6; 0 = none)
  --detail-huc8 CODES    watersheds drawn in full: every stream/canal coloured by mean velocity
                         (canals orange), width by order, flow-direction arrows, the catchment
                         of each reach, and lakes/ponds, reservoirs, swamps (NHD waterbodies)
    python scripts/map_sources.py --raw ... --year 2018 --states NC --detail-huc8 03030006
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from map_discharges import OUTFALL, fmt, outfalls, radius_fn  # noqa: E402

import numpy as np  # noqa: E402

TYPES = {"CAFO": "#f97316", "Sewage treatment plant": "#3b82f6", "Industrial facility": "#ec4899"}
STOPPED = "#9ca3af"
FIELDS = ["source_id", "year", "source_type", "status", "discharge", "lat", "lon", "detail", "animal_count",
          "npdes_id", "flow_mgd", "discharges_to", "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]



# ----------------------------------------------------------------------------- water network

FT3_TO_M3, FT_TO_M = 0.0283168, 0.3048
WB_STYLE = {"LakePond": ("#38bdf8", "Lake / pond"), "Reservoir": ("#0ea5e9", "Reservoir"),
            "SwampMarsh": ("#4ade80", "Swamp / marsh"), "Estuary": ("#a5f3fc", "Estuary")}


def wbd_huc8(raw: Path, st: str | None = None, codes=None):
    import geopandas as gpd
    out = []
    for f in sorted((raw / "wbd").glob("*/WBDHU8.parquet")):
        g = gpd.read_parquet(f, columns=["huc8", "name", "states", "geometry"])
        if codes:
            g = g[g["huc8"].isin(codes)]
        if st:
            g = g[g["states"].fillna("").str.split(",").map(lambda x: st in x)]
        out.append(g)
    return pd.concat(out).to_crs(4326) if out else None


def flowlines(raw: Path, huc4s, min_order=None, area=None):
    """Flowlines with order, Q (m3/s), velocity (m/s); filtered by order and/or an area polygon."""
    import geopandas as gpd
    frames = []
    for h in sorted(set(huc4s)):
        d = raw / "nhdplus_hr" / h
        if not (d / "NHDFlowline.parquet").exists():
            print(f"  NHDPlus HR {h} not on disk, skipped")
            continue
        fl = gpd.read_parquet(d / "NHDFlowline.parquet", columns=["NHDPlusID", "ftype_name", "LengthKM", "geometry"])
        vaa = pd.read_parquet(d / "NHDPlusFlowlineVAA.parquet", columns=["NHDPlusID", "StreamOrde"])
        erom = pd.read_parquet(d / "NHDPlusEROMMA.parquet", columns=["NHDPlusID", "QAMA", "VAMA"])
        fl = fl.merge(vaa, on="NHDPlusID", how="left").merge(erom, on="NHDPlusID", how="left")
        fl["order"] = pd.to_numeric(fl["StreamOrde"], errors="coerce").astype("float64")
        if min_order:
            fl = fl[fl["order"] >= min_order]
        fl = fl.to_crs(4326)
        if area is not None:
            fl = fl[fl.intersects(area)]
        frames.append(fl)
    if not frames:
        return None
    fl = pd.concat(frames, ignore_index=True).drop_duplicates("NHDPlusID")
    fl["q_m3s"] = pd.to_numeric(fl["QAMA"], errors="coerce").astype("float64") * FT3_TO_M3
    fl["v_ms"] = pd.to_numeric(fl["VAMA"], errors="coerce").astype("float64") * FT_TO_M
    fl["LengthKM"] = pd.to_numeric(fl["LengthKM"], errors="coerce").astype("float64")
    return fl


def velocity_colour(v, vmax=1.0):
    """light blue (slow) -> dark blue (fast); grey when unknown."""
    if v is None or not np.isfinite(v) or v <= 0:
        return "#94a3b8"
    t = min(v / vmax, 1.0)
    lo, hi = np.array([186, 230, 253]), np.array([12, 74, 110])
    r, g, b = (lo + (hi - lo) * t).astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


def stream_features(fl, simplify, arrows=False):
    feats, heads = [], []
    for r in fl.itertuples():
        typ = r.ftype_name or ""
        colour = "#f59e0b" if typ == "CanalDitch" else "#64748b" if typ in ("Pipeline", "Connector",
                                                                                  "UndergroundConduit") else velocity_colour(r.v_ms)
        width = 1 + 0.5 * (r.order if np.isfinite(r.order) else 1)
        props = {"reach": str(r.NHDPlusID), "type": typ, "order": "" if not np.isfinite(r.order) else int(r.order),
                 "flow_m3s": fmt(r.q_m3s), "velocity_ms": fmt(r.v_ms), "length_km": fmt(r.LengthKM),
                 "_c": colour, "_w": round(width, 1)}
        geom = r.geometry.simplify(simplify) if simplify else r.geometry
        feats.append({"type": "Feature", "geometry": geom.__geo_interface__, "properties": props})
        if arrows and r.LengthKM and r.LengthKM > 0.3:
            line = r.geometry if r.geometry.geom_type == "LineString" else max(r.geometry.geoms, key=lambda g: g.length)
            a, b = line.interpolate(0.48, normalized=True), line.interpolate(0.52, normalized=True)
            dx, dy = b.x - a.x, b.y - a.y
            n = (dx * dx + dy * dy) ** 0.5
            if n == 0:
                continue
            ux, uy, s = dx / n, dy / n, 0.0005                 # ~50 m chevron pointing downstream
            left = (b.x - s * (ux * 0.87 - uy * 0.5), b.y - s * (uy * 0.87 + ux * 0.5))
            right = (b.x - s * (ux * 0.87 + uy * 0.5), b.y - s * (uy * 0.87 - ux * 0.5))
            heads.append({"type": "Feature", "properties": {"_c": colour},
                          "geometry": {"type": "MultiLineString", "coordinates": [[left, (b.x, b.y)], [right, (b.x, b.y)]]}})
    return feats, heads


def add_network(m, raw: Path, st: str, min_order: int, detail):
    import folium
    import geopandas as gpd
    hu8 = wbd_huc8(raw, st)
    if hu8 is None or hu8.empty:
        print(f"  {st}: no WBD HUC8s on disk; no water network")
        return
    huc4s = {h[:4] for h in hu8["huc8"]}
    h8 = [{"type": "Feature", "geometry": g.simplify(0.002).__geo_interface__, "properties": {"huc8": c, "name": n}}
          for c, n, g in zip(hu8["huc8"], hu8["name"], hu8.geometry)]
    folium.GeoJson({"type": "FeatureCollection", "features": h8}, name=f"{st}: HUC8 watersheds ({len(h8)})",
                   style_function=lambda f: {"color": "#fde68a", "weight": 1.2, "fillOpacity": 0, "dashArray": "4"},
                   tooltip=folium.GeoJsonTooltip(fields=["huc8", "name"], aliases=["HUC8 (use with --detail-huc8)", "name"])
                   ).add_to(m)
    if min_order:
        fl = flowlines(raw, huc4s, min_order=min_order)
        if fl is not None and len(fl):
            feats, _ = stream_features(fl, 0.001)
            folium.GeoJson({"type": "FeatureCollection", "features": feats},
                           name=f"{st}: rivers, order >= {min_order} ({len(fl):,} reaches)",
                           style_function=lambda f: {"color": f["properties"]["_c"], "weight": f["properties"]["_w"],
                                                     "opacity": 0.9},
                           tooltip=folium.GeoJsonTooltip(fields=["type", "order", "flow_m3s", "velocity_ms", "reach"])
                           ).add_to(m)
            print(f"  {st}: {len(fl):,} river reaches of order >= {min_order}")
    det = hu8[hu8["huc8"].isin(detail or [])]
    for h in det.itertuples():
        area = h.geometry
        fl = flowlines(raw, [h.huc8[:4]], area=area)
        if fl is None or fl.empty:
            continue
        d = raw / "nhdplus_hr" / h.huc8[:4]
        cat = gpd.read_parquet(d / "NHDPlusCatchment.parquet", columns=["NHDPlusID", "AreaSqKm", "geometry"]).to_crs(4326)
        cat = cat[cat.intersects(area)]
        wb = gpd.read_parquet(d / "NHDWaterbody.parquet", columns=["NHDPlusID", "FCode", "AreaSqKm", "ftype_name", "geometry"]).to_crs(4326)
        wb = wb[wb.intersects(area)]
        cf = [{"type": "Feature", "geometry": g.simplify(0.0002).__geo_interface__,
               "properties": {"catchment": str(i), "area_km2": fmt(a)}}
              for i, a, g in zip(cat["NHDPlusID"], cat["AreaSqKm"], cat.geometry)]
        folium.GeoJson({"type": "FeatureCollection", "features": cf}, show=False,
                       name=f"{h.huc8} {h.name}: catchments ({len(cf):,})",
                       style_function=lambda f: {"color": "#e5e7eb", "weight": 0.6, "fillOpacity": 0},
                       tooltip=folium.GeoJsonTooltip(fields=["catchment", "area_km2"])).add_to(m)
        wf = [{"type": "Feature", "geometry": g.simplify(0.00005).__geo_interface__,
               "properties": {"type": WB_STYLE.get(t, ("#7dd3fc", t))[1] + (" (sewage treatment pond)" if fc == 43612 else ""),
                              "area_m2": fmt(a * 1e6), "fcode": int(fc) if pd.notna(fc) else "",
                              "_c": "#ef4444" if fc in (43612, 43624) else WB_STYLE.get(t, ("#7dd3fc", t))[0]}}
              for fc, a, t, g in zip(wb["FCode"], wb["AreaSqKm"], wb["ftype_name"], wb.geometry)]
        folium.GeoJson({"type": "FeatureCollection", "features": wf},
                       name=f"{h.huc8} {h.name}: lakes, ponds, reservoirs, swamps ({len(wf):,})",
                       style_function=lambda f: {"color": f["properties"]["_c"], "fillColor": f["properties"]["_c"],
                                                 "weight": 1, "fillOpacity": 0.45},
                       tooltip=folium.GeoJsonTooltip(fields=["type", "area_m2", "fcode"])).add_to(m)
        feats, heads = stream_features(fl, 0.00005, arrows=True)
        folium.GeoJson({"type": "FeatureCollection", "features": feats},
                       name=f"{h.huc8} {h.name}: streams and canals ({len(feats):,}; colour = velocity)",
                       style_function=lambda f: {"color": f["properties"]["_c"], "weight": f["properties"]["_w"],
                                                 "opacity": 0.95},
                       tooltip=folium.GeoJsonTooltip(fields=["type", "order", "flow_m3s", "velocity_ms", "length_km", "reach"])
                       ).add_to(m)
        folium.GeoJson({"type": "FeatureCollection", "features": heads},
                       name=f"{h.huc8} {h.name}: flow direction ({len(heads):,} arrows)",
                       style_function=lambda f: {"color": "#facc15", "weight": 2}).add_to(m)
        print(f"  detail {h.huc8} {h.name}: {len(feats):,} streams, {len(heads):,} arrows, "
              f"{len(cf):,} catchments, {len(wf):,} waterbodies")

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--size", default="total_waste_kg",
                    choices=["total_waste_kg", "nitrogen_kg", "ammonia_kg", "bacteria_count", "flow_mgd"])
    ap.add_argument("--states", nargs="+")
    ap.add_argument("--rivers-min-order", type=int, default=6, help="statewide streams of order >= N (0 = none)")
    ap.add_argument("--detail-huc8", nargs="+", help="HUC8 watersheds to draw in full (streams, arrows, catchments, ponds)")
    ap.add_argument("--out", type=Path, help="default sources_map_<year>.html")
    args = ap.parse_args()

    import folium

    root = args.raw / "sources"
    states = args.states or sorted(p.name for p in root.iterdir() if (p / "source_year.parquet").exists())
    m = folium.Map(location=[38.5, -96], zoom_start=4, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery", name="Satellite", max_zoom=19).add_to(m)
    for st in states:
        f = root / st / "source_year.parquet"
        if not f.exists():
            print(f"{st}: {f} not found, skipped")
            continue
        add_network(m, args.raw, st, args.rivers_min_order, args.detail_huc8)
        df = pd.read_parquet(f)
        df = df[(df["year"] == args.year) & df["lat"].notna() & df["lon"].notna()]
        rad = radius_fn(df.loc[df["discharge"] == "measured", args.size])
        measured = df[df["discharge"] == "measured"]
        lines = outfalls(args.raw, st, measured.rename(columns={"source_id": "_sid"}))
        if lines:
            folium.GeoJson({"type": "FeatureCollection", "features": lines},
                           name=f"{st}: discharge points ({len(lines) // 2:,})",
                           marker=folium.CircleMarker(radius=3, weight=1, fill=True, fill_opacity=1),
                           style_function=lambda f: {"color": OUTFALL, "fillColor": OUTFALL, "weight": 1.5},
                           tooltip=folium.GeoJsonTooltip(fields=["npdes_id", "outfall", "discharges_to", "lat", "lon"])
                           ).add_to(m)
        summary = []
        for typ, colour in TYPES.items():
            sub = df[df["source_type"] == typ].sort_values(args.size, na_position="first")
            if sub.empty:
                continue
            feats = []
            for r in sub[FIELDS].itertuples(index=False):
                row = dict(zip(FIELDS, r))
                dis = row["discharge"]
                style = ({"color": "#111827", "fillColor": colour, "fillOpacity": 0.85, "weight": 0.8,
                          "radius": rad(row[args.size])} if dis == "measured" else
                         {"color": colour, "fillOpacity": 0, "weight": 1.6, "radius": 4} if dis == "unknown" else
                         {"color": STOPPED, "fillColor": STOPPED, "fillOpacity": 0.6, "weight": 0.5, "radius": 3})
                feats.append({"type": "Feature",
                              "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
                              "properties": {**{k: (f"{v:.5f}" if k in ("lat", "lon") else fmt(v)) for k, v in row.items()},
                                             "_s": style}})
            counts = sub["discharge"].value_counts()
            label = ", ".join(f"{counts.get(k, 0):,} {k}" for k in ("measured", "unknown", "none") if counts.get(k, 0))
            folium.GeoJson({"type": "FeatureCollection", "features": feats},
                           name=f"{st} {typ}: {len(sub):,} ({label})",
                           marker=folium.CircleMarker(radius=4, fill=True),
                           style_function=lambda f: f["properties"]["_s"],
                           tooltip=folium.GeoJsonTooltip(fields=["source_id", "detail", "discharge", "discharges_to", args.size]),
                           popup=folium.GeoJsonPopup(fields=FIELDS, max_width=420)).add_to(m)
            summary.append(f"{typ} {len(sub):,}")
        print(f"{st} {args.year}: " + ", ".join(summary) + f"; {len(lines) // 2:,} discharge points")
    folium.LayerControl(collapsed=False).add_to(m)

    def dot(c, filled=True):
        return (f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;"
                f"{'background:' + c + ';border:1px solid #111' if filled else 'border:2px solid ' + c}'></span>")
    legend = "".join(f"<div>{dot(c)} {t}</div>" for t, c in TYPES.items())
    legend += (f"<div style='margin-top:4px'>{dot('#666')} discharge measured (size = {args.size})</div>"
               f"<div>{dot('#666', False)} discharge unknown</div><div>{dot(STOPPED)} stopped (none)</div>"
               f"<div><span style='display:inline-block;width:14px;border-top:2px solid {OUTFALL}'></span>"
               f"<span style='color:{OUTFALL}'>●</span> discharge point</div>"
               "<div style='margin-top:4px'><span style='display:inline-block;width:40px;height:6px;"
               "background:linear-gradient(90deg,#bae6fd,#0c4a6e)'></span> stream, slow → fast (velocity)</div>"
               "<div><span style='display:inline-block;width:14px;border-top:3px solid #f59e0b'></span> canal / ditch"
               " &nbsp; <span style='color:#facc15'>➤</span> flow direction</div>"
               "<div><span style='display:inline-block;width:10px;height:10px;background:#38bdf8'></span> lake / pond "
               "<span style='display:inline-block;width:10px;height:10px;background:#4ade80'></span> swamp "
               "<span style='display:inline-block;width:10px;height:10px;background:#ef4444'></span> sewage pond</div>")
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:24px;left:12px;z-index:9999;background:white;padding:8px 10px;"
        f"border:1px solid #999;font:12px sans-serif'><b>Pollution sources, {args.year}</b>{legend}</div>"))
    out = args.out or Path(f"sources_map_{args.year}.html")
    m.save(str(out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
