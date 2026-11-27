#!/usr/bin/env python3
"""Map the discharges dataset (raw/discharges/<ST>/facility_year.parquet) for one year.

    python scripts/map_discharges.py --raw /scratch/.../raw --year 2024
    python scripts/map_discharges.py --raw /scratch/.../raw --year 2018 --size nitrogen_kg --states NC IA

Drawn on a satellite basemap, one set of layers per state:
  * NPDES facilities: colour = facility type, grey = stopped that year; dot size = the
    `--size` column (default total_waste_kg, log scale; small = no value). Click for the
    facility's row; hover shows where it discharges and the size value.
  * Discharge points: a line from each facility to its outfall(s) (ECHO feature coordinates),
    ending in a dot where the wastewater enters the water; shown for facilities that report
    a discharge. Outfalls of facilities without discharge data (mostly stormwater: many per
    site, into ditches and storm pipes) are a separate layer, off by default.
  * Animal feeding operations from the state lists: NC DEQ (NC), Iowa DNR (IA),
    Cal-FF (CA), when those folders exist.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

GROUPS = [  # (label, colour, test on facility_type)
    ("Sewage treatment plant", "#3b82f6", lambda t: t.startswith("Sewage treatment plant")),
    ("Animal feeding operation (NPDES)", "#f97316", lambda t: t.startswith("Animal feeding operation")),
    ("Industrial stormwater", "#a855f7", lambda t: t.startswith("Industrial stormwater")),
    ("Construction site stormwater", "#facc15", lambda t: t.startswith("Construction site")),
    ("Municipal stormwater (MS4)", "#2dd4bf", lambda t: t.startswith("Municipal stormwater")),
    ("Other industry / facility", "#f472b6", lambda t: True),
]
STOPPED = "#9ca3af"
OUTFALL = "#22d3ee"
CAFO = "#fb923c"
FIELDS = ["npdes_id", "year", "status", "lat", "lon", "facility_type", "flow_mgd", "discharges_to",
          "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]
# state CAFO lists: (label, file, id, name, [popup columns], lat col or None, lon col or None)
CAFO_LISTS = {
    "NC": ("NC DEQ permitted animal facilities", "nc_deq/animal_facilities.parquet",
           ["permit_number", "facility_name", "regulated_operation", "regulated_activity", "allowable_count",
            "number_of_lagoons", "county_name"]),
    "IA": ("Iowa DNR animal feeding operations", "ia_dnr/afo_facilities.parquet",
           ["progid", "facName", "opStatus", "OperatType", "AnimalUnit", "Swine", "CattleDair", "CattleBeef",
            "Chickens", "Turkeys", "countyName"]),
    "CA": ("Cal-FF animal feeding operations (satellite census)", "ca_cafo/cal_ff_facilities.parquet",
           ["facility_id", "animal_type", "n_buildings", "footprint_sq_m", "county", "construction_upper"]),
}


def group_of(t):
    t = str(t or "")
    for label, colour, test in GROUPS:
        if test(t):
            return label, colour
    return GROUPS[-1][0], GROUPS[-1][1]


def fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (float, np.floating)):
        return f"{v:,.3g}" if abs(v) >= 1e6 else f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


def radius_fn(values: pd.Series):
    """log-scaled radius 4..18 between the 5th and 99th percentile of the positive values; 3 if none."""
    v = pd.to_numeric(values, errors="coerce")
    pos = v[v > 0]
    if pos.empty:
        return lambda x: 3
    lo, hi = math.log10(pos.quantile(0.05)), math.log10(pos.quantile(0.99))
    span = max(hi - lo, 1e-9)
    return lambda x: 3 if not (isinstance(x, (int, float)) and x > 0) else \
        round(4 + 14 * min(max((math.log10(x) - lo) / span, 0), 1), 1)


def _dms(x: pd.Series) -> pd.Series:
    """Packed DDD.MMSS -> decimal degrees; values whose minutes or seconds part is >= 60 are
    taken as already decimal."""
    d = np.floor(x)
    r = (x - d) * 100
    mi = np.floor(r + 1e-9)
    s = (r - mi) * 100
    packed = (mi < 60) & (s < 60 + 1e-6)
    return pd.Series(np.where(packed, d + mi / 60 + s / 3600, x), index=x.index)


def outfalls(raw: Path, st: str, fac: pd.DataFrame):
    """Line features facility -> outfall for the facilities in `fac` (ECHO feature coordinates)."""
    f = raw / "echo" / st / "NPDES_PERM_FEATURE_COORDS.parquet"
    if not f.exists():
        return []
    c = pd.read_parquet(f)
    # ECHO stores these as packed degrees-minutes-seconds (36.4506 = 36°45'06"), without the
    # minus sign for west longitudes; checked for NC: as DMS the median outfall is 0.18 km
    # from its facility (90% within 2 km), read as decimals 33 km.
    c["lat"] = _dms(pd.to_numeric(c["LATITUDE_MEASURE"], errors="coerce"))
    c["lon"] = -_dms(pd.to_numeric(c["LONGITUDE_MEASURE"], errors="coerce").abs())
    c = c.dropna(subset=["lat", "lon"]).merge(
        fac[["npdes_id", "lat", "lon", "discharges_to"]].rename(columns={"npdes_id": "EXTERNAL_PERMIT_NMBR"}),
        on="EXTERNAL_PERMIT_NMBR", suffixes=("", "_f"))
    # distance facility -> outfall (km); skip implausible (> 5 km) and identical points
    d = 111.2 * np.hypot(c["lat"] - c["lat_f"], (c["lon"] - c["lon_f"]) * np.cos(np.radians(c["lat"])))
    c = c[(d > 0.02) & (d < 5)]
    out = []
    for r in c.itertuples():
        props = {"npdes_id": r.EXTERNAL_PERMIT_NMBR, "outfall": r.PERM_FEATURE_NMBR,
                 "discharges_to": r.discharges_to if isinstance(r.discharges_to, str) else "",
                 "lat": f"{r.lat:.5f}", "lon": f"{r.lon:.5f}"}
        out.append({"type": "Feature", "properties": props,
                    "geometry": {"type": "LineString", "coordinates": [[r.lon_f, r.lat_f], [r.lon, r.lat]]}})
        out.append({"type": "Feature", "properties": props,
                    "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]}})
    return out


def cafo_layer(raw: Path, st: str):
    if st not in CAFO_LISTS:
        return None, 0
    import geopandas as gpd
    label, rel, cols = CAFO_LISTS[st]
    f = raw / rel
    if not f.exists():
        return None, 0
    g = gpd.read_parquet(f).to_crs(4326)
    pts = g.geometry.representative_point() if (g.geom_type != "Point").any() else g.geometry
    cols = [c for c in cols if c in g]
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(p.x, 6), round(p.y, 6)]},
              "properties": {c: fmt(v) for c, v in zip(cols, vals)}}
             for p, vals in zip(pts, g[cols].itertuples(index=False)) if p is not None and not p.is_empty]
    return (label, cols, feats), len(feats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True, help="download folder (`out` in configs/sources.yaml)")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--size", default="total_waste_kg",
                    choices=["total_waste_kg", "nitrogen_kg", "ammonia_kg", "bacteria_count", "flow_mgd"],
                    help="column that sets the dot size")
    ap.add_argument("--states", nargs="+", help="default: every state in raw/discharges/")
    ap.add_argument("--out", type=Path, help="default discharges_map_<year>.html")
    args = ap.parse_args()

    import folium

    root = args.raw / "discharges"
    states = args.states or sorted(p.name for p in root.iterdir() if (p / "facility_year.parquet").exists())
    m = folium.Map(location=[38.5, -96], zoom_start=4, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery", name="Satellite", max_zoom=19).add_to(m)
    for st in states:
        f = root / st / "facility_year.parquet"
        if not f.exists():
            print(f"{st}: {f} not found, skipped")
            continue
        df = pd.read_parquet(f)
        df = df[(df["year"] == args.year) & df["lat"].notna() & df["lon"].notna()]
        rad = radius_fn(df[args.size])
        feats = []
        # small first, so big dischargers are drawn on top
        for r in df.sort_values(args.size, na_position="first")[FIELDS].itertuples(index=False):
            row = dict(zip(FIELDS, r))
            _, colour = group_of(row["facility_type"])
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
                          "properties": {**{k: (f"{v:.5f}" if k in ("lat", "lon") else fmt(v)) for k, v in row.items()},
                                         "_c": STOPPED if row["status"] == "stopped" else colour,
                                         "_r": rad(row[args.size])}})
        # discharge points: shown for facilities that report a discharge (flow or pollutant values);
        # stormwater-only outfalls (many per site, into ditches/storm pipes) in a layer that is off
        reporting = set(df.loc[df[["flow_mgd", "nitrogen_kg", "ammonia_kg", "bacteria_count",
                                   "total_waste_kg"]].notna().any(axis=1), "npdes_id"])
        lines = outfalls(args.raw, st, df)
        for name, sel, show in (("discharge points", True, True), ("stormwater / other outfalls", False, False)):
            fs = [x for x in lines if (x["properties"]["npdes_id"] in reporting) == sel]
            if fs:
                folium.GeoJson({"type": "FeatureCollection", "features": fs}, name=f"{st}: {name} ({len(fs) // 2:,})",
                               show=show, marker=folium.CircleMarker(radius=3, weight=1, fill=True, fill_opacity=1),
                               style_function=lambda f: {"color": OUTFALL, "fillColor": OUTFALL, "weight": 1.5,
                                                         "opacity": 0.9},
                               tooltip=folium.GeoJsonTooltip(fields=["npdes_id", "outfall", "discharges_to", "lat", "lon"])
                               ).add_to(m)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            name=f"{st} {args.year}: {len(df):,} NPDES facilities (size = {args.size})",
            marker=folium.CircleMarker(radius=3, weight=0.8, fill=True, fill_opacity=0.8),
            style_function=lambda f: {"color": "#111827", "fillColor": f["properties"]["_c"],
                                      "radius": f["properties"]["_r"]},
            tooltip=folium.GeoJsonTooltip(fields=["npdes_id", "facility_type", "status", "discharges_to", args.size]),
            popup=folium.GeoJsonPopup(fields=FIELDS, max_width=420),
        ).add_to(m)
        layer, n = cafo_layer(args.raw, st)
        if layer:
            label, cols, cf = layer
            folium.GeoJson({"type": "FeatureCollection", "features": cf},
                           name=f"{st}: {label} ({n:,})", show=True,
                           marker=folium.CircleMarker(radius=3, weight=1.5, fill=False),
                           style_function=lambda f: {"color": CAFO},
                           tooltip=folium.GeoJsonTooltip(fields=cols[:3]),
                           popup=folium.GeoJsonPopup(fields=cols, max_width=380)).add_to(m)
        print(f"{st} {args.year}: {len(df):,} NPDES facilities ({int((df['status'] == 'stopped').sum()):,} stopped), "
              f"{len(lines) // 2:,} outfalls, {n:,} animal feeding operations")
    folium.LayerControl(collapsed=False).add_to(m)
    dot = "<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:{};border:1px solid #111'></span>"
    legend = "".join(f"<div>{dot.format(c)} {l}</div>" for l, c, _ in GROUPS)
    legend += f"<div>{dot.format(STOPPED)} stopped (any type)</div>"
    legend += f"<div style='margin-top:4px'>dot size = {args.size} (log scale)</div>"
    legend += (f"<div><span style='display:inline-block;width:14px;border-top:2px solid {OUTFALL}'></span>"
               f"<span style='color:{OUTFALL}'>●</span> line to discharge point (outfall)</div>")
    legend += (f"<div><span style='display:inline-block;width:9px;height:9px;border-radius:50%;"
               f"border:2px solid {CAFO}'></span> animal feeding operation (state list)</div>")
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:24px;left:12px;z-index:9999;background:white;padding:8px 10px;"
        f"border:1px solid #999;font:12px sans-serif'><b>Discharges, {args.year}</b>{legend}</div>"))
    out = args.out or Path(f"discharges_map_{args.year}.html")
    m.save(str(out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
