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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from map_discharges import OUTFALL, fmt, outfalls, radius_fn  # noqa: E402

TYPES = {"CAFO": "#f97316", "Sewage treatment plant": "#3b82f6", "Industrial facility": "#ec4899"}
STOPPED = "#9ca3af"
FIELDS = ["source_id", "year", "source_type", "status", "discharge", "lat", "lon", "detail", "animal_count",
          "npdes_id", "flow_mgd", "discharges_to", "nitrogen_kg", "ammonia_kg", "bacteria_count", "total_waste_kg"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--size", default="total_waste_kg",
                    choices=["total_waste_kg", "nitrogen_kg", "ammonia_kg", "bacteria_count", "flow_mgd"])
    ap.add_argument("--states", nargs="+")
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
               f"<span style='color:{OUTFALL}'>●</span> discharge point</div>")
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:24px;left:12px;z-index:9999;background:white;padding:8px 10px;"
        f"border:1px solid #999;font:12px sans-serif'><b>Pollution sources, {args.year}</b>{legend}</div>"))
    out = args.out or Path(f"sources_map_{args.year}.html")
    m.save(str(out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
