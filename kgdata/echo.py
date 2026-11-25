"""EPA ECHO ICIS-NPDES bulk download: permitted facilities, permits, inspections, violations.

Source: https://echo.epa.gov/files/echodownloads/npdes_downloads.zip (~360 MB zipped,
~2.3 GB of CSVs, refreshed weekly). The whole zip is downloaded; the tables are then
filtered to the requested states and written as Parquet.

NPDES permit IDs start with the two-letter state code (e.g. NCA000001), so every
table keyed by NPDES_ID / EXTERNAL_PERMIT_NMBR is filtered on that prefix.
NPDES_VIOLATION_ENFORCEMENTS has no permit ID; it is filtered through the
violation IDs kept from the violation tables.

Output: <out>/echo/<TABLE>.parquet
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


def _filter_csv(zf: zipfile.ZipFile, member: str, mask_fn) -> pd.DataFrame:
    parts = []
    with zf.open(member) as fh:
        for chunk in pd.read_csv(fh, dtype=str, chunksize=500_000, encoding="latin-1",
                                 on_bad_lines="warn"):
            parts.append(chunk[mask_fn(chunk)])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def run(out: Path, states: list[str], keep_zip: bool = True) -> None:
    s = session()
    folder = Path(out) / "echo"
    zp = download(s, URL, folder / "npdes_downloads.zip")
    prefixes = tuple(st.upper() for st in states)
    tag = "_".join(sorted(prefixes))
    log(f"[ECHO] filtering to {prefixes}")
    violation_ids: set[str] = set()
    with zipfile.ZipFile(zp) as zf:
        for table, col in KEYED.items():
            dest = folder / tag / f"{table}.parquet"
            if dest.exists() and table not in VIOLATION_TABLES:
                log(f"  have {dest}")
                continue
            df = _filter_csv(zf, f"{table}.csv",
                             lambda c, col=col: c[col].fillna("").str[:2].str.upper().isin(prefixes))
            if table in VIOLATION_TABLES:
                violation_ids |= set(df["NPDES_VIOLATION_ID"].dropna())
            dest.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(dest, index=False)
            log(f"  wrote {dest} ({len(df):,} rows)")
        dest = folder / tag / "NPDES_VIOLATION_ENFORCEMENTS.parquet"
        df = _filter_csv(zf, "NPDES_VIOLATION_ENFORCEMENTS.csv",
                         lambda c: c["NPDES_VIOLATION_ID"].isin(violation_ids))
        df.to_parquet(dest, index=False)
        log(f"  wrote {dest} ({len(df):,} rows)")
    if not keep_zip:
        zp.unlink()
