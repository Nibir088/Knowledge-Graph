#!/usr/bin/env python3
"""Map the discharges dataset (raw/discharges/<ST>/facility_year.parquet) for one year.

    python scripts/map_discharges.py --raw /scratch/.../raw --year 2024
    python scripts/map_discharges.py --raw /scratch/.../raw --year 2018 --states NC IA --out map_2018.html

One dot per facility: colour = facility type, grey = stopped that year. Click a dot for its
row (exactly the dataset's columns). One layer per state; nothing else is drawn.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

GROUPS = [  # (label, colour, test on facility_type)
    ("Sewage treatment plant", "#1d4ed8", lambda t: t.startswith("Sewage treatment plant")),
    ("Animal feeding operation", "#b45309", lambda t: t.startswith("Animal feeding operation")),
    ("Industrial stormwater", "#7c3aed", lambda t: t.startswith("Industrial stormwater")),
    ("Construction site stormwater", "#ca8a04", lambda t: t.startswith("Construction site")),
    ("Municipal stormwater (MS4)", "#0d9488", lambda t: t.startswith("Municipal stormwater")),
    ("Other industry / facility", "#db2777", lambda t: True),
]
STOPPED = "#9ca3af"
FIELDS = ["npdes_id", "year", "status", "lat", "lon", "facility_type", "flow_mgd", "discharges_to",
          "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]


def group_of(t):
    t = str(t or "")
    for label, colour, test in GROUPS:
        if test(t):
            return label, colour
    return GROUPS[-1][0], GROUPS[-1][1]


def fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float):
        return f"{v:,.3g}" if abs(v) >= 1e6 else f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True, help="download folder (`out` in configs/sources.yaml)")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--states", nargs="+", help="default: every state in raw/discharges/")
    ap.add_argument("--out", type=Path, help="default discharges_map_<year>.html")
    args = ap.parse_args()

    import folium

    root = args.raw / "discharges"
    states = args.states or sorted(p.name for p in root.iterdir() if (p / "facility_year.parquet").exists())
    m = folium.Map(location=[38.5, -96], zoom_start=4, tiles="OpenStreetMap", control_scale=True)
    counts = {}
    for st in states:
        f = root / st / "facility_year.parquet"
        if not f.exists():
            print(f"{st}: {f} not found, skipped")
            continue
        df = pd.read_parquet(f)
        df = df[(df["year"] == args.year) & df["lat"].notna() & df["lon"].notna()]
        feats = []
        for r in df[FIELDS].itertuples(index=False):
            row = dict(zip(FIELDS, r))
            label, colour = group_of(row["facility_type"])
            stopped = row["status"] == "stopped"
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
                          "properties": {**{k: (f"{v:.5f}" if k in ("lat", "lon") else fmt(v)) for k, v in row.items()},
                                         "_c": STOPPED if stopped else colour}})
        counts[st] = (len(df), int((df["status"] == "stopped").sum()))
        folium.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            name=f"{st} {args.year}: {len(df):,} facilities",
            marker=folium.CircleMarker(radius=3, weight=0.5, fill=True, fill_opacity=0.85),
            style_function=lambda f: {"color": f["properties"]["_c"], "fillColor": f["properties"]["_c"]},
            tooltip=folium.GeoJsonTooltip(fields=["npdes_id", "facility_type", "status"]),
            popup=folium.GeoJsonPopup(fields=FIELDS, max_width=420),
        ).add_to(m)
        print(f"{st} {args.year}: {len(df):,} facilities ({counts[st][1]:,} stopped)")
    folium.LayerControl(collapsed=False).add_to(m)
    legend = "".join(f"<div><span style='color:{c};font-size:16px'>●</span> {l}</div>" for l, c, _ in GROUPS)
    legend += f"<div><span style='color:{STOPPED};font-size:16px'>●</span> stopped (any type)</div>"
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:24px;left:12px;z-index:9999;background:white;padding:8px 10px;"
        f"border:1px solid #999;font:12px sans-serif'><b>NPDES facilities, {args.year}</b>{legend}</div>"))
    out = args.out or Path(f"discharges_map_{args.year}.html")
    m.save(str(out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
