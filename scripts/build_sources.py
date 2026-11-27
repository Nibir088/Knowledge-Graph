#!/usr/bin/env python3
"""Build the pollution source list (CAFOs, sewage treatment plants, industrial facilities).

    python scripts/build_sources.py --raw /scratch/wyr6fx/knowledge-graph/raw
    python scripts/build_sources.py --raw /scratch/wyr6fx/knowledge-graph/raw --states NC --start 2017 --end 2024

Needs the discharges step (scripts/download_sources.py --only discharges), the ECHO download and
the state CAFO lists (nc_deq, ia_dnr, ca_cafo). Writes raw/sources/<ST>/source_year.parquet.
Rules: kgdata/sources.py and docs/data_sources.md.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kgdata import sources  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--raw", type=Path, required=True)
ap.add_argument("--states", nargs="+", default=["NC", "CA", "IA"])
ap.add_argument("--start", type=int, default=2017)
ap.add_argument("--end", type=int, default=2024)
a = ap.parse_args()
sources.run(a.raw, a.states, a.start, a.end)
