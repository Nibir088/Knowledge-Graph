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
| `ca_cafo` | CA Water Boards (CIWQS) + Cal-FF (Stanford RegLab) | permitted CA CAFOs; satellite census with footprints |
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
