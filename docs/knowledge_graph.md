# Building the knowledge graph

`scripts/build_graph.py` turns the downloaded data for one state into a typed graph of
river reaches, ponds, lagoons, CAFOs, water-quality stations and NPDES facilities. It
reads only the download folders (nothing is downloaded or changed) and writes plain
Parquet tables, one per node type and one per edge type.

```bash
python scripts/build_graph.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC --out graph_NC
python scripts/build_graph.py --raw /scratch/wyr6fx/knowledge-graph/raw --state NC \
    --huc4 0302 0303 --storm 2018-09-13 2018-09-18 --out graph_NC_capefear_neuse
```

| Option | Default | Meaning |
|---|---|---|
| `--huc4` | every HUC4 WBD lists for the state | river basins to include (whole HUC4s) |
| `--storm START END` | 2018-09-13 2018-09-18 (Florence) | storm s*: Stage IV days to use |
| `--cafo-radius-m` | 200 | radius of the circle used as a farm's area for `drains_to` |
| `--snap-m` | 250 | how far a water-quality station may be from its reach |
| `--include-swamps` | off | also make pond nodes of NHD swamps/marshes |
| `--lagoon-points` | none | lagoon point file; lagoons are placed there instead of at the farm point |
| `--fields`, `--fields-id` | none | field polygons; adds `field` nodes with `drains_to` weights |

Load the result:

```python
from kgdata.graph_io import load_tables, to_networkx
nodes, edges = load_tables("graph_NC")        # dicts of DataFrames
G = to_networkx(nodes, edges)                 # networkx.MultiDiGraph (pip install networkx)
```

## Output

```
graph_NC/
  nodes/reach.parquet  pond.parquet  lagoon.parquet  cafo.parquet
        wq_station.parquet  npdes_facility.parquet  [field.parquet]
  edges/flows_to.parquet  drains_to.parquet  pond_on_reach.parquet  has_lagoon.parquet
        observes.parquet  discharges_to.parquet  same_site.parquet
  tables/wq_results.parquet      every water-quality result, with standardised units
  graph_report.md                counts, checks, assumptions, time per step
  graph_meta.json                parameters and input folder
```

Every node has an `id` of the form `<type>:<key>` (`reach:15000500012345`,
`cafo:AWS310290`, `lagoon:AWS310290:1`, `pond:<NHD Permanent_Identifier>`,
`wq_station:USGS-02105769`, `npdes_facility:NC0001422`). Edge tables have `src` and
`dst` holding those ids. Node tables carry `lon`/`lat` (EPSG:4326) of a representative
point; full geometries stay in the raw files (join on `nhdplusid`, `permanent_identifier`,
`permit_number`, `station`, `npdes_id`).

## Study area

The HUC4s are those whose HUC8s list the state in WBD's `states` column (or `--huc4`).
Whole HUC4s are used, not clipped at the state line, so the river network stays
connected; reaches in a neighbouring state are therefore part of the graph. Flow rows
that appear in two neighbouring HUC4s are de-duplicated. CAFOs and water-quality stations
are kept only when their point lies in one of the study catchments.

All areas and distances are computed in EPSG:5070 (CONUS Albers, equal area).

## Nodes

### `reach` (V_R)

One node per NHDPlus HR flowline (coastlines dropped).

| Attribute | Source / rule |
|---|---|
| `ftype`, `fcode`, `length_km`, `huc4` | NHDFlowline |
| `stream_order`, `hydroseq`, `divergence`, `in_network` | NHDPlusFlowlineVAA (`in_network` = has a VAA row) |
| `q_m3s` | mean annual flow QAMA × 0.0283168 (ft³/s → m³/s) |
| `v_ms`, `v_source` | mean annual velocity VAMA × 0.3048 (ft/s → m/s). Missing for ~25% of reaches (mostly ArtificialPath and reaches outside the EROM network): filled with the median EROM velocity of reaches with the same stream order, else the overall median; `v_source` says which |
| `tau_s` | travel time through the reach = length / velocity, seconds |
| `is_outlet`, `leaves_study_area` | the reach has a flow row to ToNHDPID 0, or to a reach outside the study HUC4s |
| `catchment_km2`, `huc12` | its NHDPlus catchment (same NHDPlusID) and the WBD HUC12 containing the catchment's representative point |
| `sfha_share` | share of the catchment area inside FEMA's SFHA (1%-annual-chance floodplain), counted on a 30 m grid (coarser only if the study area exceeds ~135,000 km²); exact polygon intersections took ~8 minutes per HUC4 |
| `r_u_in` | Atlas 14 25-yr 24-h depth at the catchment's representative point |
| `storm_total_in`, `storm_max_1day_in`, `storm_over_r_u` | Stage IV rainfall over the storm days at that point: total, wettest single day, and wettest day ÷ R_u |

### `pond` (V_P)

NHD waterbodies of FType 390 (LakePond) and 436 (Reservoir, which includes the
sewage-treatment pond FCode 43612, flagged `sewage_or_treatment_pond`). Attributes:
`area_m2`, `fcode`, `on_network` (an ArtificialPath runs through it), `fdate`.

Swamps/marshes (466) are **left out by default**: in eastern NC many NHD swamp outlines
date from old topographic maps and now cover drained pine plantations (checked on
imagery). NHD also misses most lagoons: in the Cape Fear basin only 8% of the 1,091
farms with lagoons have any NHD open-water polygon within 100 m. NHD ponds are therefore
ponds, not lagoons; lagoons are their own node type.

### `lagoon` (V_P)

One node per lagoon. Without `--lagoon-points`, the number of lagoons comes from NC DEQ's
`number_of_lagoons` and every lagoon is **placed at its farm's point**
(`location_from = "CAFO point"`), so lagoons of one farm share a location. With
`--lagoon-points` (e.g. NC's swine lagoon point layer), each point becomes a lagoon and is
linked to the nearest permitted farm within 500 m (`dist_to_cafo_m`); points with no farm
nearby are kept as `lagoon:unmatched:<n>`.

Attributes: `catchment`, `flood_zone`, `in_sfha`, `r_u_in` (the lagoon design storm R̄_u),
storm rainfall and `storm_over_r_u` (> 1 means the wettest storm day exceeded the design
storm at that point).

### `cafo`

NC DEQ permitted animal facilities (`nc_deq/animal_facilities.parquet`): `permit_number`,
`name`, `animal`, `activity`, `allowable_count`, `lagoons_reported`, `permit_type`,
`county`, `catchment`, `huc12`, `flood_zone`, `in_sfha`, `r_u_in`, storm rainfall.
Dry-litter poultry farms need no permit and are not in this list (see
`docs/data_sources.md`).

### `wq_station`

Water Quality Portal stations that have results and lie in a study catchment. For each
parameter: number of results `n_<param>` and median standardised value
`median_<param>` (detected values only), `first_date`, `last_date`, `snap_m` (distance to
the reach it observes, empty if none within `--snap-m`).

Results are in `tables/wq_results.parquet`, with units standardised:

| `param` | WQP characteristics | Standard unit | Conversions |
|---|---|---|---|
| `nitrate_n` | Nitrate; Inorganic nitrogen (nitrate and nitrite) | mg/L as N | as NO3 × 14.007/62.004; µeq/L × 0.014007 |
| `ammonia_n` | Ammonia; Ammonia and ammonium | mg/L as N | as NH4 × 14.007/18.038; as NH3 × 14.007/17.031 |
| `ecoli` | Escherichia coli | count/100 mL | cfu/100 mL and MPN/100 mL taken as equal |

A nitrogen result reported in plain `mg/L` is taken as "as N" and flagged
`unit_assumed`. Results without a value (mostly "Not Detected") keep
`detection`; they are not used in the medians.

### `npdes_facility`

EPA ECHO ICIS-NPDES facilities whose point lies in a study catchment: `npdes_id`,
`name`, `permit_type` and `permit_status` (latest permit version in ICIS_PERMITS: individual
NPDES permit, general permit coverage, …; effective, administratively continued, expired,
terminated), `construction_stormwater` (NC IDs starting NCC: construction-site stormwater
coverages, the majority of NC "facilities"), `cafo_like` (permit component CAF or livestock
SIC 02xx), counts of
`inspections`, `violations` (permit-schedule, compliance-schedule and single-event),
`formal_enforcements`, `informal_enforcements`.

### `field` (optional)

With `--fields`: one node per polygon, with its area and all columns of the file.

## Edges

| Edge | From → to | Attributes | Rule |
|---|---|---|---|
| `flows_to` | reach → reach | `f_e`, `Q_w_m3s`, `tau_e_s`, `f_method`, `hydroseq_ok` | one edge per NHDPlusFlow row between two study reaches (rows with FromNHDPID 0 are headwater markers and skipped) |
| `drains_to` | cafo / lagoon / pond / field → reach | `weight`, `src_type` | see below |
| `pond_on_reach` | pond → reach | | the reach's WBArea_Permanent_Identifier is the pond |
| `has_lagoon` | cafo → lagoon | | |
| `observes` | wq_station → reach | `snap_m` | nearest network reach (StreamRiver, ArtificialPath, CanalDitch, Connector) within `--snap-m` |
| `discharges_to` | npdes_facility → reach | | the reach of the catchment containing the facility point (not the outfall: outfall coordinates are in ECHO's `NPDES_PERM_FEATURE_COORDS` if needed) |
| `same_site` | cafo → npdes_facility | `distance_m` | nearest NPDES facility within 500 m |

**Split share f_e.** For a reach u with downstream reaches v₁ … vₖ:
f_e(u→vᵢ) = QAMA(vᵢ) / Σⱼ QAMA(vⱼ); with one downstream reach f_e = 1; if all QAMA are 0,
an equal split. NHDPlus HR's `DivFrac` table is not populated (all values are USGS's
"not computed" code), so it cannot be used. QAMA includes the local inflow of each
branch, so the shares are approximate; at the 2,211 splits in the Cape Fear basin the
main path gets a median share of 1.0 (mean 0.92) and the minor path a mean of 0.08.

**Discharge and travel time.** Q_w(u→v) = Q(u) × f_e (m³/s); τ_e(u→v) = travel time
through u (s). Both are mean-annual values: NHDPlus HR has no monthly or storm flows.

**drains_to weight.** weight = area(source ∩ catchment) / area(source), with the edge
going to the catchment's reach. Ponds and fields use their polygons. CAFOs use a circle
of `--cafo-radius-m` around the farm point because farm outlines are not published; a
lagoon (a point) gets weight 1 to the catchment containing it. Weights of one source sum
to less than 1 when part of it lies in a sink catchment (no reach) or outside the study
area; the report counts these.

## Checks in the report

- f_e sums to 1 for every reach that has downstream edges.
- Acyclicity: HydroSeq decreases along every `flows_to` edge (NHDPlus orders reaches
  from upstream to downstream). 0 violations means the river network has no cycles.
- No edge refers to a node that does not exist.
- How many reaches got a fallback velocity, how many CAFOs lose drains_to weight, how many
  stations are too far from a reach, which Stage IV days were missing.

## Known limitations

| Limitation | Effect | Fix later |
|---|---|---|
| Lagoons placed at the farm point | lagoon flood zone and catchment are those of the farm point | add NC's lagoon point layer (`--lagoon-points`) or digitised outlines |
| Farm area is a circle | drains_to weights are approximate | farm or field outlines (`--fields`) |
| Dry-litter poultry missing | about two thirds of NC CAFOs are not nodes | a poultry facility dataset |
| Mean-annual flow and velocity only | τ_e and Q_w are not storm values | a hydrologic model for event flows |
| Rainfall sampled at one point per catchment on a 0.02° grid | fine for ~1 km² catchments, coarse for big ones | zonal means |
| NPDES links use the facility point | discharge may enter a different reach | use outfall coordinates |
