"""Water Quality Portal (WQP): sampling stations and results for nitrate, ammonia, E. coli.

Source: https://www.waterqualitydata.us/data/{Station,Result}/search (WQX 2.2 CSV)
One folder per state; inside it, one stations file and one results file per year,
so every request stays small and a failed run resumes where it stopped.

states: "all" (50 states, DC and territories) or a list of postal codes ([NC, WA]).

Output: <out>/wqp/<ST>/stations.csv, <out>/wqp/<ST>/results_<year>.csv
Key result fields: MonitoringLocationIdentifier, ActivityStartDate,
CharacteristicName, ResultMeasureValue, ResultMeasure/MeasureUnitCode,
ResultSampleFractionText.  Station fields: LatitudeMeasure, LongitudeMeasure, HUCEightDigitCode.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .common import get, log, session
from .states import STATES, resolve

BASE = "https://www.waterqualitydata.us/data/{kind}/search"
DEFAULT_CHARACTERISTICS = [
    "Nitrate",
    "Inorganic nitrogen (nitrate and nitrite)",
    "Ammonia",
    "Ammonia and ammonium",
    "Escherichia coli",
]


def _fetch(s, kind: str, params: list[tuple[str, str]], dest: Path) -> None:
    if dest.exists():
        log(f"  have {dest}")
        return
    r = get(s, BASE.format(kind=kind), params=params + [("mimeType", "csv"), ("zip", "yes")],
            timeout=1800)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        data = zf.read(zf.namelist()[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    nrows = max(data.count(b"\n") - 1, 0)
    log(f"  wrote {dest} ({nrows:,} rows)")


def run(out: Path, start_year: int, end_year: int, characteristics=None, states="all") -> None:
    s = session()
    chars = characteristics or DEFAULT_CHARACTERISTICS
    for st in resolve(states):
        log(f"[WQP] {st} {start_year}-{end_year}")
        base = [("statecode", f"US:{STATES[st]}")] + \
               [("characteristicName", c) for c in chars] + [("sorted", "no")]
        folder = Path(out) / "wqp" / st
        _fetch(s, "Station", base + [("startDateLo", f"01-01-{start_year}"),
                                     ("startDateHi", f"12-31-{end_year}")], folder / "stations.csv")
        for y in range(start_year, end_year + 1):
            _fetch(s, "Result", base + [("startDateLo", f"01-01-{y}"), ("startDateHi", f"12-31-{y}")],
                   folder / f"results_{y}.csv")
