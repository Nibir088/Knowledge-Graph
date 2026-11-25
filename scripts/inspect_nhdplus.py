#!/usr/bin/env python3
"""Inspect one HUC4's NHDPlus Parquet layers and create a clickable HTML map.

Install: python -m pip install geopandas pyarrow folium
Run: python inspect_nhdplus.py --data-dir /path/to/nhdplus_hr/0510 --out inspection_0510
Optional: --reach-id 24001100052602 --radius-km 2
Optional: --bbox WEST SOUTH EAST NORTH (longitude/latitude, EPSG:4326)

Reads source files only. Map covers a small inspection window by default;
the JSON report checks the entire supplied HUC4. No geometry simplification
or repairs are applied. HTML requires internet for map tiles and JS/CSS.

Reading the flow links in the popups:
  upstream_IDs / downstream_IDs   NHDPlusIDs of the neighbouring reaches
  "none (headwater)"              NHDPlusFlow says nothing flows in (FromNHDPID = 0)
  "none (outlet)"                 NHDPlusFlow says water leaves the network (ToNHDPID = 0)
  "not in network"                the reach has no NHDPlusFlow row at all
                                  (coastlines and many canals / pipelines; no VAA either)

Arrows show flow direction. NHD lines are drawn from upstream to downstream; the
report checks this against NHDPlusFlow ("orientation" section).

Ponds are coloured by how they connect to the streams:
  on network      an ArtificialPath in the flow network runs through the pond
  off network     only flowlines outside the flow network touch it
  isolated        no flowline touches it (off-channel farm pond, lagoon, ...)
"""
import argparse
import html
import json
import math
from pathlib import Path

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

SPATIAL = {"NHDFlowline", "NHDPlusCatchment", "NHDWaterbody"}
LAYERS = ["NHDFlowline", "NHDPlusFlow", "NHDPlusFlowlineVAA",
          "NHDPlusEROMMA", "NHDPlusCatchment", "NHDWaterbody"]
COLORS = {"StreamRiver": "#1665d8", "ArtificialPath": "#9333ea",
          "CanalDitch": "#e77e17", "Connector": "#64748b"}
NOT_IN_NETWORK_COLOR = "#94a3b8"
POND_COLORS = {"on network": "#22d3ee", "off network": "#a78bfa", "isolated": "#f97316"}


def summarize(name, df):
    key = (["FromNHDPID", "ToNHDPID"] if name == "NHDPlusFlow" else
           ["Permanent_Identifier"] if name == "NHDWaterbody" else ["NHDPlusID"])
    result = {"rows": len(df), "columns": list(df.columns),
              "dtypes": {c: str(t) for c, t in df.dtypes.items()},
              "missing": {c: int(v) for c, v in df.isna().sum().items()}}
    if all(c in df for c in key):
        result["duplicate_key_rows_beyond_first"] = int(df.duplicated(key).sum())
    if name in SPATIAL:
        geom = df.geometry
        present = geom.notna()
        result.update(crs=df.crs.to_string() if df.crs else None,
                      geometry_types=geom.geom_type.value_counts().to_dict(),
                      empty_geometries=int(geom.is_empty.sum()),
                      invalid_nonmissing_geometries=int((present & ~geom.is_valid).sum()))
        if present.any() and (~geom.is_empty).any():
            result["bounds_in_source_crs"] = df.total_bounds.tolist()
        if "LengthKM" in df:
            result["nonpositive_lengths"] = int((df.LengthKM <= 0).sum())
        if "ftype_name" in df:
            result["feature_types"] = df.ftype_name.fillna("<missing>").value_counts().to_dict()
    return result


def scalar(value):
    return "" if pd.isna(value) else html.escape(str(value))


def add_layer(map_, name, df, style, tooltip_fields):
    if df.empty:
        return
    df = df.to_crs(4326).copy()
    # Strings preserve IDs in browser popups and safely represent missing values.
    fields = [c for c in df.columns if c != "geometry"]
    for c in fields:
        df[c] = df[c].map(scalar)
    tip = [c for c in tooltip_fields if c in fields]
    folium.GeoJson(
        json.loads(df.to_json()), name=f"{name} ({len(df):,})",
        style_function=style, smooth_factor=0,
        highlight_function=lambda _: {"weight": 5, "fillOpacity": 0.25},
        tooltip=folium.GeoJsonTooltip(fields=tip) if tip else None,
        popup=folium.GeoJsonPopup(fields=fields, max_width=650),
    ).add_to(map_)


def _last_part(geoms):
    """Last LineString of each (Multi)LineString; LineStrings are returned as-is."""
    return shapely.get_geometry(geoms, -1)


def orientation_check(reaches, flow):
    """Share of flow edges where the upstream line ends where the downstream line starts."""
    g = reaches.dropna(subset=["NHDPlusID"]).drop_duplicates("NHDPlusID").set_index("NHDPlusID").geometry
    e = flow[(flow.FromNHDPID != 0) & (flow.ToNHDPID != 0)
             & flow.FromNHDPID.isin(g.index) & flow.ToNHDPID.isin(g.index)]
    if e.empty:
        return {"edges_checked": 0}
    a = g.loc[e.FromNHDPID.to_numpy()].to_numpy()
    b = g.loc[e.ToNHDPID.to_numpy()].to_numpy()
    a_end = shapely.get_point(_last_part(a), -1)
    b_start = shapely.get_point(shapely.get_geometry(b, 0), 0)
    a_start = shapely.get_point(shapely.get_geometry(a, 0), 0)
    b_end = shapely.get_point(_last_part(b), -1)
    tol = 1e-6  # degrees (~0.1 m)
    ok = shapely.distance(a_end, b_start) < tol
    rev = ~ok & (shapely.distance(a_start, b_end) < tol)
    return {"edges_checked": int(len(e)), "drawn_downstream": int(ok.sum()),
            "drawn_upstream": int(rev.sum()), "endpoints_do_not_meet": int((~ok & ~rev).sum())}


def pond_connection(ponds, reaches):
    """Label each waterbody: on network / off network / isolated."""
    net = reaches[reaches["in_network"]] if "in_network" in reaches else reaches
    on_ids = set(net.WBArea_Permanent_Identifier.dropna()) if "WBArea_Permanent_Identifier" in net else set()
    lines = reaches[reaches.geometry.notna()][["geometry"]].to_crs(ponds.crs)
    hit = gpd.sjoin(ponds[["geometry"]], lines, how="inner", predicate="intersects").index.unique()
    hit_net = gpd.sjoin(ponds[["geometry"]], net[["geometry"]].to_crs(ponds.crs), how="inner",
                        predicate="intersects").index.unique()
    lab = pd.Series("isolated", index=ponds.index)
    lab[ponds.index.isin(hit)] = "off network"
    lab[ponds.index.isin(hit_net) | ponds.Permanent_Identifier.isin(on_ids)] = "on network"
    return lab


ARROW_SVG = ('<div style="transform:rotate({a:.0f}deg);width:14px;height:14px">'
             '<svg viewBox="0 0 10 10" width="14" height="14"><path d="M5 0 L10 10 L5 7 L0 10 Z" '
             'fill="{fill}" stroke="#1e293b" stroke-width="0.9"/></svg></div>')


def add_arrows(map_, lines, max_arrows, spacing_m=400):
    """Flow-direction arrows along each line, pointing the way it is drawn (downstream)."""
    merc = lines.to_crs(3857)
    group = folium.FeatureGroup(name="Flow direction arrows")
    count = 0
    in_net = merc["in_network"] if "in_network" in merc else pd.Series(True, index=merc.index)
    for geom, on in zip(merc.geometry, in_net):
        if geom is None or geom.is_empty:
            continue
        line = shapely.line_merge(geom) if geom.geom_type == "MultiLineString" else geom
        length = line.length
        n = int(min(6, max(1, round(length / spacing_m))))
        for k in range(n):
            f = (k + 0.5) / n
            p1 = line.interpolate(max(f - 0.02, 0), normalized=True)
            p2 = line.interpolate(min(f + 0.02, 1), normalized=True)
            if p1.equals(p2):
                continue
            angle = math.degrees(math.atan2(p2.x - p1.x, p2.y - p1.y))  # clockwise from north
            mid = gpd.GeoSeries([line.interpolate(f, normalized=True)], crs=3857).to_crs(4326).iloc[0]
            fill = "#fde047" if bool(on) else "#cbd5e1"
            folium.Marker([mid.y, mid.x], icon=folium.DivIcon(
                html=ARROW_SVG.format(a=angle, fill=fill), icon_size=(14, 14),
                icon_anchor=(7, 7))).add_to(group)
            count += 1
            if count >= max_arrows:
                print(f"Arrow limit {max_arrows} reached; raise --max-arrows to draw more")
                group.add_to(map_)
                return count
    group.add_to(map_)
    return count


def neighbour_labels(flow, reach_ids, col, other, end_label):
    """Per reach: the neighbouring IDs on the `other` side, with 0 spelled out."""
    def fmt(s):
        vals = [int(v) for v in s.dropna().drop_duplicates()]
        real = [str(v) for v in vals if v != 0]
        return ", ".join(real) if real else end_label
    groups = flow.dropna(subset=[col]).groupby(col)[other].agg(fmt)
    return reach_ids.map(groups).fillna("not in network")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("nhd_inspection"))
    ap.add_argument("--reach-id", type=int)
    ap.add_argument("--radius-km", type=float, default=2)
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--max-features", type=int, default=15000,
                    help="Map limit per layer; errors rather than silently dropping features")
    ap.add_argument("--max-arrows", type=int, default=5000, help="Limit on flow-direction arrows")
    ap.add_argument("--no-arrows", action="store_true", help="Do not draw flow-direction arrows")
    args = ap.parse_args()
    if args.radius_km <= 0 or args.max_features < 1:
        ap.error("radius-km and max-features must be positive")
    args.out.mkdir(parents=True, exist_ok=True)
    frames, report = {}, {"source": str(args.data_dir.resolve()), "layers": {}}
    for name in LAYERS:
        path = args.data_dir / f"{name}.parquet"
        if not path.exists():
            print(f"MISSING: {path}")
            report["layers"][name] = {"missing_file": True}
            continue
        print(f"Reading {path}", flush=True)
        df = gpd.read_parquet(path) if name in SPATIAL else pd.read_parquet(path)
        frames[name] = df
        report["layers"][name] = summarize(name, df)
        preview = df.drop(columns="geometry", errors="ignore").head(20).copy()
        if name in SPATIAL:
            preview["geometry_wkt"] = df.geometry.head(20).to_wkt().values
        preview.to_csv(args.out / f"{name}_first20.csv", index=False)
        print(f"  {len(df):,} rows; {len(df.columns)} columns")
    if "NHDFlowline" not in frames:
        ap.error("NHDFlowline.parquet is required")
    reaches = frames["NHDFlowline"].copy()
    if reaches.crs is None:
        ap.error("Flowlines have no CRS; cannot locate them on a map")

    # Network membership: a reach is in the routed network if NHDPlusFlow mentions it.
    if "NHDPlusFlow" in frames:
        flow = frames["NHDPlusFlow"]
        ids = set(reaches.NHDPlusID.dropna().astype(int))
        report["connectivity"] = {}
        for col in ("FromNHDPID", "ToNHDPID"):
            values = flow[col]
            outside = values.notna() & ~values.isin(ids) & (values != 0)
            report["connectivity"][col] = {
                "missing": int(values.isna().sum()), "zero": int((values == 0).sum()),
                "nonzero_rows_not_in_this_flowline_file": int(outside.sum()),
                "examples": [str(v) for v in values[outside].drop_duplicates().head(10)]}
        report["connectivity"]["note"] = (
            "FromNHDPID = 0 marks a headwater, ToNHDPID = 0 an outlet. Unmatched endpoints may "
            "reference adjacent HUC4s or unresolved source connections; they do not alone prove "
            "extraction errors.")
        in_flow = set(flow.FromNHDPID.dropna()) | set(flow.ToNHDPID.dropna())
        reaches["in_network"] = reaches.NHDPlusID.isin(in_flow)
        off = reaches[~reaches.in_network]
        report["connectivity"]["reaches_not_in_network"] = {
            "count": len(off),
            "by_type": off.ftype_name.fillna("<missing>").value_counts().to_dict()
            if "ftype_name" in off else {}}
        reaches["upstream_IDs"] = neighbour_labels(flow, reaches.NHDPlusID, "ToNHDPID",
                                                   "FromNHDPID", "none (headwater)")
        reaches["downstream_IDs"] = neighbour_labels(flow, reaches.NHDPlusID, "FromNHDPID",
                                                     "ToNHDPID", "none (outlet)")
        report["orientation"] = orientation_check(reaches, flow)
        print(f"Line direction check: {report['orientation']}")

    if "NHDWaterbody" in frames:
        ponds = frames["NHDWaterbody"].copy()
        ponds["connection"] = pond_connection(ponds, reaches)
        frames["NHDWaterbody"] = ponds
        report["waterbody_connection"] = {
            "counts": ponds.connection.value_counts().to_dict(),
            "by_type": {k: v.connection.value_counts().to_dict()
                        for k, v in ponds.groupby(ponds.get("ftype_name", pd.Series("all", index=ponds.index)))},
            "note": "Isolated waterbodies touch no flowline. In the graph they can be linked "
                    "to the reach of the catchment they lie in (overland connection)."}
        print(f"Waterbody connection: {report['waterbody_connection']['counts']}")

    # Add source attributes to reach popups without assuming all features have them.
    for name in ("NHDPlusFlowlineVAA", "NHDPlusEROMMA"):
        if name not in frames:
            continue
        attrs = frames[name].drop(columns="huc4", errors="ignore").copy()
        if attrs.NHDPlusID.duplicated().any():
            print(f"WARNING: {name} has duplicate NHDPlusIDs; not added to reach popups")
            continue
        # Nullable ints, so reaches without a row do not turn 1 into 1.0
        for c in attrs.columns:
            if pd.api.types.is_integer_dtype(attrs[c]):
                attrs[c] = attrs[c].astype("Int64")
        reaches = reaches.merge(attrs, on="NHDPlusID", how="left", validate="many_to_one")

    usable = reaches[reaches.geometry.notna() & ~reaches.geometry.is_empty]
    if args.reach_id is not None:
        focus = usable[usable.NHDPlusID == args.reach_id]
        if focus.empty:
            ap.error(f"Reach {args.reach_id} not found or has no usable geometry")
    else:
        # Default: the first stream reach that is part of the network
        cand = usable
        if "in_network" in cand:
            cand = cand[cand.in_network]
        if "ftype_name" in cand and (cand.ftype_name == "StreamRiver").any():
            cand = cand[cand.ftype_name == "StreamRiver"]
        focus = (cand if len(cand) else usable).head(1)
    if focus.empty:
        ap.error("No usable flowline geometries")
    selected_id = int(focus.NHDPlusID.iloc[0])

    if args.bbox:
        west, south, east, north = args.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            ap.error("bbox must be WEST SOUTH EAST NORTH in degrees; no antimeridian crossing")
        window = gpd.GeoSeries([box(west, south, east, north)], crs=4326)
    else:
        utm = focus.estimate_utm_crs()
        if utm is None:
            ap.error("Cannot estimate local projection; supply --bbox")
        metric = focus.to_crs(utm)
        center = metric.geometry.iloc[0].representative_point()
        window = gpd.GeoSeries([center.buffer(args.radius_km * 1000)], crs=utm).to_crs(4326)
    bounds = window.total_bounds.tolist()
    report["map"] = {"selected_reach": str(selected_id), "window_bounds_epsg4326": bounds,
                     "selection": "Intersecting whole features; geometries are not clipped or simplified",
                     "counts": {}}
    report_path = args.out / "inspection_report.json"
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
    west, south, east, north = bounds
    m = folium.Map(location=[(south+north)/2, (west+east)/2], tiles="OpenStreetMap",
                   prefer_canvas=True, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery", name="Satellite imagery", overlay=False).add_to(m)
    specs = [
        ("NHDPlusCatchment", {"color": "#a37500", "weight": 1, "fillColor": "#eac54f", "fillOpacity": 0.08}),
        ("NHDWaterbody", "ponds"),
        ("NHDFlowline", None),
    ]
    for name, style in specs:
        if name not in frames:
            continue
        df = reaches if name == "NHDFlowline" else frames[name]
        if df.crs is None:
            ap.error(f"{name} has no CRS")
        area = window.to_crs(df.crs).iloc[0]
        subset = df[df.geometry.notna() & ~df.geometry.is_empty & df.intersects(area)].copy()
        report["map"]["counts"][name] = len(subset)
        if len(subset) > args.max_features:
            ap.error(f"{name}: {len(subset):,} map features exceeds limit. Reduce radius/bbox or raise --max-features")
        subset.drop(columns="geometry").to_csv(args.out / f"{name}_map_attributes.csv", index=False)
        print(f"Mapping {name}: {len(subset):,} features", flush=True)
        if style is None:
            def style_fn(feature):
                p = feature["properties"]
                selected = p.get("NHDPlusID") == str(selected_id)
                off = p.get("in_network") == "False"
                color = ("#e11d48" if selected else NOT_IN_NETWORK_COLOR if off
                         else COLORS.get(p.get("ftype_name"), "#1665d8"))
                return {"color": color, "weight": 6 if selected else 2.5, "opacity": 0.95,
                        "dashArray": "4,4" if off else None}
        elif style == "ponds":
            def style_fn(feature):
                c = POND_COLORS.get(feature["properties"].get("connection"), "#0891b2")
                return {"color": c, "weight": 2, "fillColor": c, "fillOpacity": 0.4}
        else:
            def style_fn(feature, fixed=style):
                return fixed
        add_layer(m, name, subset, style_fn,
                  ["NHDPlusID", "ftype_name", "connection", "LengthKM", "AreaSqKm"])
        if name == "NHDFlowline" and not args.no_arrows:
            n = add_arrows(m, subset, args.max_arrows)
            report["map"]["arrows"] = n
            print(f"Drew {n:,} flow-direction arrows", flush=True)
    folium.GeoJson(json.loads(window.to_json()), name="Inspection window", smooth_factor=0,
                   style_function=lambda _: {"color": "#334155", "weight": 2, "dashArray": "5,5", "fillOpacity": 0}).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.fit_bounds([[south, west], [north, east]])
    legend = f"""<div style="position:fixed;bottom:28px;left:12px;z-index:9999;background:white;
    padding:12px;border:1px solid #aaa;border-radius:6px;font:13px Arial;max-width:310px">
    <b>NHDPlus manual inspection</b><br>Selected reach: {selected_id}<br>
    <span style="color:#e11d48">&#9679; Selected reach</span> &nbsp;
    <span style="color:#1665d8">&#9679; Stream</span><br>
    <span style="color:#9333ea">&#9679; Artificial path</span> &nbsp;
    <span style="color:#e77e17">&#9679; Canal/ditch</span><br>
    <span style="color:{NOT_IN_NETWORK_COLOR}">&#9476; Not in flow network (dashed)</span><br>
    <span style="color:#eab308">&#9650;</span> Flow direction (grey = not in network)<br>
    Ponds: <span style="color:#22d3ee">&#9632; on network</span>
    <span style="color:#a78bfa">&#9632; off network</span>
    <span style="color:#f97316">&#9632; isolated</span><br>
    Gold: catchments<br>
    Hover for IDs; click for attributes and flow links.<br>
    Toggle layers or choose Satellite imagery above.<br>
    Whole intersecting features are shown. The window limits data selection;
    panning beyond it does not load more features.<br>
    Connectivity and source attributes are not independently validated.
    </div>"""
    m.get_root().html.add_child(folium.Element(legend))
    map_path = args.out / "nhd_map.html"
    m.save(str(map_path))
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False, default=str))
    print(f"\nMap: {map_path.resolve()}\nReport: {report_path.resolve()}")
    print("Download nhd_map.html and open it in an internet-connected browser.")


if __name__ == "__main__":
    main()
