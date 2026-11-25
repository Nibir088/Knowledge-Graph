"""EPA ECHO ICIS-NPDES bulk download: permitted facilities, permits, inspections, violations.

Source: https://echo.epa.gov/files/echodownloads/npdes_downloads.zip (~360 MB zipped,
~2.3 GB of CSVs, refreshed weekly). The whole zip is downloaded, then every table is
converted to Parquet, either for the whole United States (states: all) or only for
the listed states.

NPDES permit IDs start with the two-letter state code (e.g. NCA000001), so every
table keyed by NPDES_ID / EXTERNAL_PERMIT_NMBR is filtered on that prefix.
NPDES_VIOLATION_ENFORCEMENTS has no permit ID; it is filtered through the
violation IDs kept from the violation tables.

Tables are read and written in chunks, so the national files never have to fit in
memory. All columns are kept as text (IDs keep leading zeros); convert dates and
numbers when you use them.

Output: <out>/echo/ALL/<TABLE>.parquet           states: all
        <out>/echo/<ST_ST..>/<TABLE>.parquet     states: [NC, WA, ...]
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from .common import download, log, session

URL = "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip"

# table -> permit-id column used for the state filter
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


def _csv_to_parquet(zf: zipfile.ZipFile, member: str, dest: Path, mask_fn=None,
                    collect: str | None = None, chunksize: int = 500_000) -> set[str]:
    """Stream one CSV from the zip into a Parquet file, optionally filtering rows.

    Returns the set of values of column `collect` among the kept rows (if given).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    writer, schema, rows, seen = None, None, 0, set()
    try:
        with zf.open(member) as fh:
            for chunk in pd.read_csv(fh, dtype=str, chunksize=chunksize, encoding="latin-1",
                                     on_bad_lines="warn", keep_default_na=False, na_values=[""]):
                if mask_fn is not None:
                    chunk = chunk[mask_fn(chunk)]
                if collect:
                    seen |= set(chunk[collect].dropna())
                if schema is None:
                    schema = pa.schema([(c, pa.string()) for c in chunk.columns])
                    writer = pq.ParquetWriter(tmp, schema, compression="zstd")
                if len(chunk):
                    writer.write_table(pa.Table.from_pandas(chunk, schema=schema, preserve_index=False))
                    rows += len(chunk)
        if writer is None:  # empty CSV: write an empty file with no columns
            pd.DataFrame().to_parquet(tmp, index=False)
    finally:
        if writer is not None:
            writer.close()
    tmp.replace(dest)
    log(f"  wrote {dest} ({rows:,} rows)")
    return seen


def run(out: Path, states="all", keep_zip: bool = True) -> None:
    """states: "all" (whole US) or a list of two-letter state codes."""
    s = session()
    folder = Path(out) / "echo"
    zp = download(s, URL, folder / "npdes_downloads.zip")
    all_states = states in (None, "all", "ALL", []) or (isinstance(states, str) and states.lower() == "all")
    if all_states:
        prefixes, tag = None, "ALL"
        log("[ECHO] all states")
    else:
        prefixes = tuple(st.upper() for st in ([states] if isinstance(states, str) else states))
        tag = "_".join(sorted(prefixes))
        log(f"[ECHO] filtering to {prefixes}")

    violation_ids: set[str] = set()
    with zipfile.ZipFile(zp) as zf:
        for table, col in KEYED.items():
            dest = folder / tag / f"{table}.parquet"
            needs_ids = table in VIOLATION_TABLES and not all_states
            if dest.exists() and not needs_ids:
                log(f"  have {dest}")
                continue
            if needs_ids and dest.exists():  # only re-read the violation IDs
                violation_ids |= set(pd.read_parquet(dest, columns=["NPDES_VIOLATION_ID"])
                                     ["NPDES_VIOLATION_ID"].dropna())
                continue
            mask = None if all_states else (
                lambda c, col=col: c[col].fillna("").str[:2].str.upper().isin(prefixes))
            violation_ids |= _csv_to_parquet(zf, f"{table}.csv", dest, mask,
                                             collect="NPDES_VIOLATION_ID" if needs_ids else None)

        dest = folder / tag / "NPDES_VIOLATION_ENFORCEMENTS.parquet"
        if dest.exists():
            log(f"  have {dest}")
        else:
            mask = None if all_states else (lambda c: c["NPDES_VIOLATION_ID"].isin(violation_ids))
            _csv_to_parquet(zf, "NPDES_VIOLATION_ENFORCEMENTS.csv", dest, mask)
    if not keep_zip:
        zp.unlink()
