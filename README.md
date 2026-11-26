# Knowledge Graph

Hydrological knowledge graph linking reaches, ponds, catchments, CAFOs and
agricultural fields.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Data sources

### NHDPlus HR

`scripts/download_nhdplus_hr.py` downloads the USGS NHDPlus High Resolution
File Geodatabases (one per HUC4) from the National Map S3 bucket and writes the
layers below to `raw/nhdplus_hr/<HUC4>/<layer>.parquet`.

| Layer | Fields read | Becomes |
|---|---|---|
| `NHDFlowline` | NHDPlusID, FType, LengthKM, WBArea_Permanent_Identifier | reach nodes V_R; reach–pond links |
| `NHDPlusFlow` | FromNHDPID → ToNHDPID | `flows_to` edges |
| `NHDPlusFlowlineVAA` | Divergence, HydroSeq, StreamOrde | branch split share f_e; acyclicity check |
| `NHDPlusEROMMA` | QAMA (flow), VAMA (velocity) | discharge Q_w; travel time τ_e |
| `NHDPlusCatchment` | polygon + NHDPlusID | `drains_to` weight f = overlap area ÷ CAFO or field area |
| `NHDWaterbody` | Permanent_Identifier, FType, AreaSqKm | pond nodes V_P |

```bash
python scripts/download_nhdplus_hr.py --list                      # available HUC4s and sizes
python scripts/download_nhdplus_hr.py --huc4 0710 0708 --merge    # selected regions
python scripts/download_nhdplus_hr.py --huc2 07 --mask study_area.gpkg --merge --delete-gdb --delete-zip
```

Full method, step by step: [docs/nhdplus_hr_download.md](docs/nhdplus_hr_download.md).

Notes:

- Units: QAMA in ft³/s, VAMA in ft/s, LengthKM in km, so
  τ_e [s] = LengthKM · 1000 / (VAMA · 0.3048).
- EROM sentinel values (-9998, -9999) are written as NaN; some VPUs have no
  EROM estimates.
- `Divergence` is 0 (no split), 1 (main path) or 2 (minor path); it gives no
  split fraction, so f_e must be assigned (e.g. by discharge or equal split).
- NHDPlus HR is no longer updated by USGS; the staged files date from 2023.

### All other sources

`scripts/download_sources.py` downloads the remaining inputs, configured in
`configs/sources.yaml`:

| Key | Dataset | Gives you |
|---|---|---|
| `wbd` | USGS Watershed Boundary Dataset | HUC8/10/12 polygons |
| `atlas14` | NOAA Atlas 14 | lagoon design storm R̄_u (25-yr, 24-h depth) |
| `stage4` | NOAA Stage IV (NWPS) | observed daily rainfall for storm s* |
| `nfhl` | FEMA National Flood Hazard Layer | floodplain polygons |
| `wqp` | Water Quality Portal | nitrate, ammonia, E. coli samples |
| `echo` | EPA ECHO ICIS-NPDES | permitted facilities, inspections, violations |
| `nc_deq` | NC DEQ permitted animal facilities | CAFO IDs, type, lagoon counts |
| `ia_dnr` | Iowa DNR animal feeding operations | every IA AFO/CAFO, animal units, head counts by species |
| `ca_cafo` | CA Water Boards (CIWQS) + Cal-FF (Stanford RegLab) + CARB CADD | permitted CA CAFOs with violations; satellite census with footprints; dairy herd sizes |
| `discharges` | EPA DMR + Water Pollutant Loading Tool + ECHO | every NPDES facility per year: operating/stopped, location, type, flow, receiving water, nitrogen, ammonia, bacteria, total waste (NC, CA, IA; 2017–2024) |
| `wa_ecology` | WA Ecology water rights | points of diversion, places of use |
| `ca_calwatrs` | CA water rights (CalWATRS, replaces eWRIMS) | points of diversion, places of use |

```bash
python scripts/download_sources.py                     # everything in the config
python scripts/download_sources.py --only nc_deq wqp   # selected sources
```

Every source is collected for the whole United States; data that comes per state
(FEMA flood zones, water quality, ECHO) is stored one folder per state, e.g.
`raw/wqp/NC/`, so a study can pick its states. Set `out` in `configs/sources.yaml` first.
How each source is downloaded, with its gaps: [docs/data_sources.md](docs/data_sources.md).

## Checking what we have for one state

`scripts/inspect_state.py` reads the download folder (it changes nothing) and shows
everything collected for one state, for manual verification:

```bash
python scripts/inspect_state.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC
python scripts/inspect_state.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC \
    --focus-huc4 0302 0303 --date 2018-09-16 --out inspect_NC
```

It writes to `inspect_<ST>/`:

| File | What it is |
|---|---|
| `report.md` | one section per dataset (OK / PARTIAL / MISSING / N/A): counts, types, date ranges, units, and a "check by hand" list |
| `cafo_links.csv` | one row per CAFO with what each dataset says at that spot: HUC12, NHDPlus catchment, nearest NHD pond and its type, nearest stream and its order, Atlas 14 25-yr 24-h depth, Stage IV rainfall on `--date`, FEMA flood zone, nearest water-quality station, nearest ECHO NPDES facility |
| `samples.md` (+ `samples/*.csv`) | 10 records per dataset (`--samples`), each with links to check it against the source: Google satellite view, FEMA flood map, NOAA Atlas 14 table, ECHO facility report, WQP station page. CAFO samples include up to 3 in the floodplain; rainfall samples are the 10 wettest CAFOs on `--date` |
| `map.html` | interactive map: CAFOs (click for all the links above, plus satellite / FEMA / NOAA links), satellite imagery to check lagoons, rainfall on `--date`, Atlas 14 grid, FEMA SFHA within 2 km of CAFOs, NHDPlus streams and nearby ponds, WQP stations, ECHO CAFO permits, HUC8 outlines |

The state's area comes from WBD's `states` column. NHDPlus links, streams and flood
zones are computed for the focus HUC4s only (default: the two with the most CAFOs);
everything else covers the whole state. `--min-order` sets the smallest stream order
drawn (default 4); lower values make the map heavier. Rainfall days that exist only
as GRIB (2016, 2019, 2020) are read from the tar and converted from mm to inches.

Flood zones are read one row group at a time: a statewide file is large (NC: 183,013
polygons, 3.2 GB of geometry, more than 14 GB once loaded in geopandas). For NC the whole
check takes about a minute and under 4 GB of memory.

## Building the knowledge graph

`scripts/build_graph.py` turns the downloaded data for a state into node and edge tables
(Parquet). Method, every rule and the known limitations: [docs/knowledge_graph.md](docs/knowledge_graph.md).

```bash
python scripts/build_graph.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC --out graph_NC
python scripts/build_graph.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC \
    --huc4 0302 0303 --storm 2018-09-13 2018-09-18 --out graph_NC_capefear_neuse
```

| Nodes | Edges |
|---|---|
| `reach` (V_R, NHDPlus flowlines) | `flows_to` reach→reach: split share f_e, discharge Q_w, travel time τ_e |
| `pond` (V_P, NHD lakes/ponds/reservoirs) | `drains_to` cafo/lagoon/pond/field→reach: overlap-area weight |
| `lagoon` (V_P, from NC DEQ lagoon counts) | `pond_on_reach`, `has_lagoon` |
| `cafo` (NC DEQ permits) | `observes` wq_station→reach |
| `wq_station` (WQP) | `discharges_to` npdes_facility→reach |
| `npdes_facility` (ECHO) | `same_site` cafo→npdes_facility |

Reaches, CAFOs and lagoons carry flood-zone (FEMA SFHA), design-storm (Atlas 14 R_u) and
storm rainfall (Stage IV, `--storm`) attributes. Load with
`kgdata.graph_io.load_tables("graph_NC")`, or `to_networkx(...)` for a networkx graph.
For the Cape Fear basin (HUC4 0303) the build takes about 2.5 minutes and 5.5 GB of memory.

### Map of the discharges dataset

```bash
python scripts/map_discharges.py --raw /scratch/wyr6fx/knowledge-graph/raw --year 2024
```

Writes `discharges_map_<year>.html`: one dot per facility (colour = facility type, grey =
stopped that year), one layer per state; clicking a dot shows its row of
`facility_year.parquet`. `--states NC IA` limits the states, `--year` picks the year.
