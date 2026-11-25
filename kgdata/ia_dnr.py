"""Iowa DNR animal feeding operations (AFOs/CAFOs), plus optional karst/drainage context layers.

Source: Iowa DNR AFO siting map service (live copy of the state AFO database)
    https://programs.iowadnr.gov/geospatial/rest/services/Agriculture/AnimalFeedingOperations/MapServer
  layer 3  Facilities: every AFO known to DNR (about 14,300; about 12,800 active), confinements
           and open feedlots of all sizes. Columns include opStatus, OperatType, AnimalUnit,
           head counts (Swine, CattleDair, CattleBeef, Chickens, Turkeys, SheepLGoat, Horses,
           Other), HUC (HUC12), permit/plan flags (ConsPermit, NPDESPermit, MMP, NMP),
           Latitude/Longitude and how the location was collected.
Optional:
  afo_300au           Iowa DNR hosted layer of active AFOs with >= 300 animal units
                      (about 10,700), with a finer species/stage split (SwineSow, SwineNurs,
                      SwineGrow, DairyHol, BeefFin, ChickLay, TurkeyFin, ...). It is a
                      snapshot, last edited May 2024.
  ag_drainage_wells   agricultural drainage wells (layer 1, about 390 points)
  sinkholes           mapped sinkholes (layer 0, about 41,000 points)
  drainage_districts  drainage district polygons (layer 11, about 6,900)

Keys: in afo_facilities, `progid` (the DNR facility ID used in the AFO database and eMMP
links) is unique per row. OBJECTID and LOCID identify a location: 69 locations carry
two program records (e.g. an old inactive and a new active operation), so both rows
are kept. `coord_ok` is False for the few points outside Iowa (swapped lat/lon in
the source); they are kept as published.

Only about 1.5% of Iowa AFOs hold an NPDES permit, so ECHO misses almost all of them;
this list is the one to use for Iowa.

Output: <out>/ia_dnr/<layer>.parquet (EPSG:4326). Each run re-reads the live service;
an existing file is kept unless refresh is true.
"""

from __future__ import annotations

from pathlib import Path

from .common import arcgis_layer, flag_bbox, log, session, write_gdf

MS = "https://programs.iowadnr.gov/geospatial/rest/services/Agriculture/AnimalFeedingOperations/MapServer"
LAYERS = {
    "afo_facilities": f"{MS}/3",
    "afo_300au": "https://services3.arcgis.com/C5aIa0c3gUMji8fg/ArcGIS/rest/services/"
                 "animal_feeding_operations/FeatureServer/0",
    "ag_drainage_wells": f"{MS}/1",
    "sinkholes": f"{MS}/0",
    "drainage_districts": f"{MS}/11",
}
DEFAULT = ["afo_facilities", "afo_300au"]
IOWA_BBOX = (-96.7, 40.3, -90.1, 43.6)


def run(out: Path, layers=None, refresh: bool = False) -> None:
    s = session()
    folder = Path(out) / "ia_dnr"
    for name in layers or DEFAULT:
        if name not in LAYERS:
            raise ValueError(f"unknown ia_dnr layer {name}; choose from {list(LAYERS)}")
        path = folder / f"{name}.parquet"
        if path.exists() and not refresh:
            log(f"  have {path}")
            continue
        log(f"[IA DNR] {name}")
        gdf = arcgis_layer(s, LAYERS[name])
        gdf = flag_bbox(gdf, IOWA_BBOX)
        write_gdf(gdf, path)
