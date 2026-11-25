"""EPA ECHO ICIS-NPDES bulk download: permitted facilities, permits, inspections, violations.

Source: https://echo.epa.gov/files/echodownloads/npdes_downloads.zip (~360 MB zipped,
~2.3 GB of CSVs, refreshed weekly). The whole zip is downloaded, then every table is
split into one Parquet file per state.

The state comes from the NPDES permit ID, which starts with the two-letter state code
(e.g. NCA000001 -> NC). Codes that are not states also occur (tribal and other
permits); they get their own folders too. NPDES_VIOLATION_ENFORCEMENTS has no permit
ID; its rows go to the state of the violation they belong to (from the three
violation tables), or to _unknown.

states: "all" (every code found) or a list, e.g. [NC, WA].
Tables are streamed in chunks, so the national files never have to fit in memory.
All columns are kept as text (IDs keep leading zeros).

Output: <out>/echo/<ST>/<TABLE>.parquet
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from .common import download, log, session

URL = "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip"

# table -> permit-id column that carries the state
KEYED = {
    "ICIS_FACILITIES": "NPDES_ID",
    "ICIS_PERMITS": "EXTERNAL_PERMIT_NMBR",
    "NPDES_NAICS": "NPDES_ID",
    "NPDES_SICS": "NPDES_ID",
    "NPDES_INSPECTIONS": "NPDES_ID",
    "NPDES_PS_VIOLATIONS": "NPDES_ID",
    "NPDES_CS_VIOLATIONS": "NPDES_ID",
    "NPDES_SE_VIOLATIONS": "NPDES_ID",
    "NPDES_FORMAL_ENFORCEMENT_ACTIONS": "NPDES_ID",
    "NPDES_INFORMAL_ENFORCEMENT_ACTIONS": "NPDES_ID",
    "NPDES_QNCR_HISTORY": "NPDES_ID",
    "NPDES_PERM_COMPONENTS": "EXTERNAL_PERMIT_NMBR",
    "NPDES_PERM_FEATURE_COORDS": "EXTERNAL_PERMIT_NMBR",
    "NPDES_DATA_GROUPS": "EXTERNAL_PERMIT_NMBR",
}
VIOLATION_TABLES = ("NPDES_PS_VIOLATIONS", "NPDES_CS_VIOLATIONS", "NPDES_SE_VIOLATIONS")
UNKNOWN = "_unknown"


def _split_csv(zf, member: str, root: Path, table: str, state_fn, keep, chunksize=500_000):
    """Stream one CSV into <root>/<ST>/<table>.parquet; returns {state: rows}."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    writers, counts, schema = {}, {}, None
    try:
        with zf.open(member) as fh:
            for chunk in pd.read_csv(fh, dtype=str, chunksize=chunksize, encoding="latin-1",
                                     on_bad_lines="warn", keep_default_na=False, na_values=[""]):
                if schema is None:
                    schema = pa.schema([(c, pa.string()) for c in chunk.columns])
                st = state_fn(chunk)
                for code, part in chunk.groupby(st, sort=False):
                    if keep is not None and code not in keep:
                        continue
                    if code not in writers:
                        path = root / code / f"{table}.parquet.part"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        writers[code] = pq.ParquetWriter(path, schema, compression="zstd")
                    writers[code].write_table(pa.Table.from_pandas(part, schema=schema,
                                                                   preserve_index=False))
                    counts[code] = counts.get(code, 0) + len(part)
    finally:
        for w in writers.values():
            w.close()
    for code in writers:
        part = root / code / f"{table}.parquet.part"
        part.replace(part.with_suffix(""))
    return counts


def run(out: Path, states="all", keep_zip: bool = True) -> None:
    s = session()
    folder = Path(out) / "echo"
    zp = download(s, URL, folder / "npdes_downloads.zip")
    keep = None
    if not (states is None or (isinstance(states, str) and states.lower() == "all")):
        keep = {x.upper() for x in ([states] if isinstance(states, str) else states)}
    log(f"[ECHO] splitting by state ({'all' if keep is None else sorted(keep)})")
    done_marker = folder / ".split_done"
    if done_marker.exists() and done_marker.read_text() == zp.name + str(zp.stat().st_size) + str(sorted(keep or [])):
        log("  already split for this download")
        return

    violation_state: dict[str, str] = {}
    with zipfile.ZipFile(zp) as zf:
        for table, col in KEYED.items():
            def state_of(c, col=col):
                return c[col].fillna("").str[:2].str.upper().replace("", UNKNOWN)
            if table in VIOLATION_TABLES:  # remember violation -> state
                def state_of(c, col=col, f=state_of):
                    st = f(c)
                    violation_state.update(zip(c["NPDES_VIOLATION_ID"], st))
                    return st
            counts = _split_csv(zf, f"{table}.csv", folder, table, state_of, keep)
            log(f"  {table}: {sum(counts.values()):,} rows in {len(counts)} state folders")

        def ve_state(c):
            return c["NPDES_VIOLATION_ID"].map(violation_state).fillna(UNKNOWN)
        counts = _split_csv(zf, "NPDES_VIOLATION_ENFORCEMENTS.csv", folder,
                            "NPDES_VIOLATION_ENFORCEMENTS", ve_state, keep)
        log(f"  NPDES_VIOLATION_ENFORCEMENTS: {sum(counts.values()):,} rows in {len(counts)} state folders")
    done_marker.write_text(zp.name + str(zp.stat().st_size) + str(sorted(keep or [])))
    if not keep_zip:
        zp.unlink()
