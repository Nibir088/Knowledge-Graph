#!/usr/bin/env python3
"""Download the knowledge-graph data sources listed in configs/sources.yaml.

    python scripts/download_sources.py                       # every source in the config
    python scripts/download_sources.py --only nc_deq wqp     # selected sources
    python scripts/download_sources.py --config my.yaml --out /data/raw

Each source skips files it already has, so a failed run can simply be restarted.
Sources: wbd, atlas14, stage4, nfhl, wqp, echo, nc_deq, wa_ecology, ca_calwatrs.
NHDPlus HR has its own script: scripts/download_nhdplus_hr.py.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kgdata import atlas14, ca_calwatrs, echo, nc_deq, nfhl, stage4, wa_ecology, wbd, wqp  # noqa: E402


def run_source(name: str, cfg: dict, out: Path) -> None:
    if name == "wbd":
        wbd.run(out, cfg["huc2"], cfg.get("levels", [8, 10, 12]))
    elif name == "atlas14":
        atlas14.grids(out, cfg["regions"], cfg.get("return_period", 25), cfg.get("duration", "24h"))
        if cfg.get("points"):
            atlas14.points(out, Path(cfg["points"]), cfg.get("lat_col", "lat"),
                           cfg.get("lon_col", "lon"), cfg.get("id_col"))
    elif name == "stage4":
        stage4.run(out, cfg["start"], cfg.get("end", "latest"), cfg.get("area", "conus"),
                   cfg.get("fmt", "tif"), cfg.get("source_tar", False), cfg.get("fallback_tar", True))
    elif name == "nfhl":
        nfhl.run(out, cfg.get("mode", "states"), cfg.get("states", "all"), cfg.get("keep_zip", True),
                 cfg.get("bbox"), cfg.get("only_sfha", False), cfg.get("batch", 10), cfg.get("clip", True))
    elif name == "wqp":
        wqp.run(out, cfg["start_year"], cfg["end_year"], cfg.get("characteristics"),
                cfg.get("states", "all"))
    elif name == "echo":
        echo.run(out, cfg.get("states", "all"))
    elif name == "nc_deq":
        nc_deq.run(out)
    elif name == "wa_ecology":
        wa_ecology.run(out)
    elif name == "ca_calwatrs":
        ca_calwatrs.run(out, cfg.get("legacy_ewrims", False))
    else:
        raise ValueError(f"unknown source {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("configs/sources.yaml"))
    ap.add_argument("--only", nargs="+", help="run only these sources")
    ap.add_argument("--out", type=Path, help="override `out` from the config")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text())
    out = args.out or Path(cfg.pop("out", "raw"))
    cfg.pop("out", None)
    names = args.only or list(cfg)
    failed = []
    for name in names:
        if name not in cfg:
            print(f"[{name}] not in {args.config}; skipped")
            continue
        try:
            run_source(name, cfg[name] or {}, out)
        except Exception:  # keep going with the other sources
            traceback.print_exc()
            failed.append(name)
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    print("\nall requested sources done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
