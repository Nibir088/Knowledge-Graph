#!/usr/bin/env python3
"""Download NHDPlus HR (High Resolution) and extract the layers for the hydro graph.

Source: USGS National Map staged products on S3, one File Geodatabase per HUC4
(VPU), e.g.
    https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/NHDPLUS_H_1710_HU4_GDB.zip

For every HUC4 the script
  1. downloads the zip (resumable, retried, size-checked),
  2. extracts the .gdb,
  3. reads only the layers / fields below and writes them as (Geo)Parquet:

    layer                 fields                                                    -> becomes
    NHDFlowline           NHDPlusID, FType, FCode, LengthKM,                        reach nodes V_R,
                          WBArea_Permanent_Identifier, Permanent_Identifier         reach-pond links
    NHDPlusFlow           FromNHDPID, ToNHDPID                                      flows_to edges
    NHDPlusFlowlineVAA    NHDPlusID, Divergence, HydroSeq, StreamOrde               split share f_e, acyclicity
    NHDPlusEROMMA         NHDPlusID, QAMA, VAMA                                     Q_w, tau_e = LengthKM / VAMA
    NHDPlusCatchment      NHDPlusID, AreaSqKm, geometry                             drains_to weights
    NHDWaterbody          Permanent_Identifier, NHDPlusID, FType, FCode,            pond nodes V_P
                          AreaSqKm, geometry

Output layout:
    <out>/<HUC4>/<layer>.parquet           per HUC4
    <out>/merged/<layer>.parquet           with --merge (all requested HUC4s)

Units / nodata (as published by USGS): QAMA in ft^3/s, VAMA in ft/s, LengthKM in km,
so travel time in seconds is tau_e = LengthKM * 1000 / (VAMA * 0.3048). EROM
sentinel values (-9998, -9999) are written as NaN. Islands / some VPUs have no
EROM values at all (empty table).

Geometries are 2D, CRS is the source CRS (NAD83, EPSG:4269). With --bbox or
--mask, spatial layers are clipped to that area and the attribute tables are
reduced to the NHDPlusIDs of the kept flowlines.

Examples
    python download_nhdplus_hr.py --list                       # show available HUC4s + sizes
    python download_nhdplus_hr.py --huc4 0710 0708 --out raw/nhdplus_hr
    python download_nhdplus_hr.py --huc2 07 --merge --delete-gdb
    python download_nhdplus_hr.py --huc4 1710 --bbox -123.5 45.0 -122.5 46.0

Requires: requests, tqdm, geopandas, pyogrio, pyarrow
"""

from __future__ import annotations

import argparse
import re
import warnings
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests
from tqdm import tqdm

S3_BUCKET_URL = "https://prd-tnm.s3.amazonaws.com"
S3_PREFIX = "StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/"
ZIP_RE = re.compile(r"NHDPLUS_H_(\d{4})_HU4_GDB\.zip$")

# layer -> (fields to keep, has geometry)
LAYERS: dict[str, tuple[list[str], bool]] = {
    "NHDFlowline": (
        ["NHDPlusID", "Permanent_Identifier", "FType", "FCode", "LengthKM",
         "WBArea_Permanent_Identifier"],
        True,
    ),
    "NHDPlusFlow": (["FromNHDPID", "ToNHDPID"], False),
    "NHDPlusFlowlineVAA": (["NHDPlusID", "Divergence", "HydroSeq", "StreamOrde"], False),
    "NHDPlusEROMMA": (["NHDPlusID", "QAMA", "VAMA"], False),
    "NHDPlusCatchment": (["NHDPlusID", "AreaSqKm"], True),
    "NHDWaterbody": (
        ["Permanent_Identifier", "NHDPlusID", "FType", "FCode", "AreaSqKm"],
        True,
    ),
}

# NHD FType codes -> names (added as `ftype_name` for readability)
FLOWLINE_FTYPES = {
    334: "Connector", 336: "CanalDitch", 420: "UndergroundConduit",
    428: "Pipeline", 460: "StreamRiver", 558: "ArtificialPath", 566: "Coastline",
}
WATERBODY_FTYPES = {
    361: "Playa", 378: "IceMass", 390: "LakePond", 436: "Reservoir",
    466: "SwampMarsh", 493: "Estuary",
}


# --------------------------------------------------------------------------- #
# Listing / download
# --------------------------------------------------------------------------- #
def list_available(session: requests.Session) -> dict[str, int]:
    """Return {HUC4: zip size in bytes} from the S3 bucket listing (paginated)."""
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    out: dict[str, int] = {}
    token = None
    while True:
        params = {"list-type": "2", "prefix": S3_PREFIX}
        if token:
            params["continuation-token"] = token
        r = session.get(S3_BUCKET_URL + "/", params=params, timeout=120)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.findall("s3:Contents", ns):
            key = c.findtext("s3:Key", namespaces=ns)
            m = ZIP_RE.search(key or "")
            if m:
                out[m.group(1)] = int(c.findtext("s3:Size", namespaces=ns))
        if root.findtext("s3:IsTruncated", namespaces=ns) == "true":
            token = root.findtext("s3:NextContinuationToken", namespaces=ns)
        else:
            return dict(sorted(out.items()))


def zip_url(huc4: str) -> str:
    return f"{S3_BUCKET_URL}/{S3_PREFIX}NHDPLUS_H_{huc4}_HU4_GDB.zip"


def download(session: requests.Session, url: str, dest: Path,
             retries: int = 8, chunk: int = 1 << 20) -> Path:
    """Resumable download with exponential-backoff retries and a size check."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = int(session.head(url, timeout=60, allow_redirects=True).headers["Content-Length"])
    if dest.exists() and dest.stat().st_size == total:
        print(f"  have {dest.name} ({total / 1e9:.2f} GB)")
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        if have > total:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(url, headers=headers, stream=True, timeout=120) as r:
                r.raise_for_status()
                if have and r.status_code != 206:  # server ignored Range: restart
                    have = 0
                mode = "ab" if have else "wb"
                with open(part, mode) as f, tqdm(
                    total=total, initial=have, unit="B", unit_scale=True,
                    desc=f"  {dest.name}", leave=False,
                ) as bar:
                    for block in r.iter_content(chunk):
                        f.write(block)
                        bar.update(len(block))
            if part.stat().st_size == total:
                part.rename(dest)
                return dest
            raise IOError(f"incomplete: {part.stat().st_size}/{total} bytes")
        except Exception as exc:  # network errors, truncated transfers
            wait = min(2 ** attempt * 5, 300)
            print(f"  download error ({exc}); retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"failed to download {url}")


def extract_gdb(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the .gdb folder from the zip (skipped if already extracted)."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        gdb_names = sorted({n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n})
        if len(gdb_names) != 1:
            raise RuntimeError(f"expected one .gdb in {zip_path}, found {gdb_names}")
        gdb = dest_dir / gdb_names[0]
        marker = gdb / ".extracted"
        if marker.exists():
            return gdb
        print(f"  extracting {gdb.name}")
        for n in names:
            target = (dest_dir / n).resolve()
            if not str(target).startswith(str(dest_dir.resolve())):  # zip-slip guard
                raise RuntimeError(f"unsafe path in zip: {n}")
        zf.extractall(dest_dir, members=[n for n in names if ".gdb/" in n])
    marker.touch()
    return gdb


# --------------------------------------------------------------------------- #
# Layer extraction
# --------------------------------------------------------------------------- #
def resolve_columns(gdb: Path, layer: str, wanted: list[str]) -> dict[str, str]:
    """Map wanted field names to the GDB's actual names (case-insensitive)."""
    import pyogrio

    fields = list(pyogrio.read_info(gdb, layer=layer)["fields"])
    lower = {f.lower(): f for f in fields}
    found, missing = {}, []
    for w in wanted:
        if w.lower() in lower:
            found[w] = lower[w.lower()]
        else:
            missing.append(w)
    if missing:
        print(f"    WARNING {layer}: fields not found {missing}; available: {fields}")
    return found


def read_layer(gdb: Path, layer: str, fields: list[str], spatial: bool, bbox=None):
    import geopandas as gpd
    import pandas as pd
    import pyogrio

    colmap = resolve_columns(gdb, layer, fields)
    kwargs = dict(layer=layer, columns=list(colmap.values()), use_arrow=False)
    if spatial:
        df = pyogrio.read_dataframe(gdb, bbox=bbox, **kwargs)
        df = df.set_geometry(gpd.GeoSeries(df.geometry, crs=df.crs).force_2d())
    else:
        df = pyogrio.read_dataframe(gdb, read_geometry=False, **kwargs)
        df = pd.DataFrame(df)
    return df.rename(columns={v: k for k, v in colmap.items()})


def extract_layers(gdb: Path, out_dir: Path, huc4: str, bbox=None, mask=None) -> None:
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    frames = {}
    for layer, (fields, spatial) in LAYERS.items():
        print(f"    reading {layer}")
        df = read_layer(gdb, layer, fields, spatial, bbox=bbox if spatial else None)
        if spatial and mask is not None:
            df = df[df.intersects(mask.to_crs(df.crs).union_all())]
        frames[layer] = df

    if bbox is not None or mask is not None:  # keep only tables tied to kept reaches
        ids = set(frames["NHDFlowline"]["NHDPlusID"])
        flow = frames["NHDPlusFlow"]
        frames["NHDPlusFlow"] = flow[flow["FromNHDPID"].isin(ids) | flow["ToNHDPID"].isin(ids)]
        for t in ("NHDPlusFlowlineVAA", "NHDPlusEROMMA"):
            frames[t] = frames[t][frames[t]["NHDPlusID"].isin(ids)]

    for layer, ftypes in (("NHDFlowline", FLOWLINE_FTYPES), ("NHDWaterbody", WATERBODY_FTYPES)):
        if "FType" in frames[layer]:
            frames[layer]["ftype_name"] = frames[layer]["FType"].map(ftypes)

    erom = frames["NHDPlusEROMMA"]
    for c in ("QAMA", "VAMA"):
        if c in erom:
            erom[c] = erom[c].where(erom[c] > -9000)  # -9998 / -9999 = not computed

    vaa = frames["NHDPlusFlowlineVAA"]
    if "StreamOrde" in vaa:  # -9 = no stream order assigned (e.g. coastline reaches)
        vaa["StreamOrde"] = pd.to_numeric(vaa["StreamOrde"]).where(lambda s: s >= 0).astype("Int64")

    for layer, df in frames.items():
        df = df.copy()
        df["huc4"] = huc4
        for c in ("NHDPlusID", "FromNHDPID", "ToNHDPID", "HydroSeq"):  # IDs are 14-digit floats in the GDB
            if c in df:
                df[c] = pd.to_numeric(df[c]).round().astype("Int64")
        path = out_dir / f"{layer}.parquet"
        df.to_parquet(path, index=False)
        print(f"    wrote {path} ({len(df):,} rows)")


def merge_outputs(out_root: Path, hucs: list[str]) -> None:
    import geopandas as gpd
    import pandas as pd

    merged = out_root / "merged"
    merged.mkdir(parents=True, exist_ok=True)
    for layer, (_, spatial) in LAYERS.items():
        paths = [out_root / h / f"{layer}.parquet" for h in hucs]
        paths = [p for p in paths if p.exists()]
        if not paths:
            continue
        reader = gpd.read_parquet if spatial else pd.read_parquet
        df = pd.concat([reader(p) for p in paths], ignore_index=True)
        key = ["FromNHDPID", "ToNHDPID"] if layer == "NHDPlusFlow" else (
            ["Permanent_Identifier"] if layer == "NHDWaterbody" else ["NHDPlusID"])
        df = df.drop_duplicates(subset=key)
        if spatial:
            df = gpd.GeoDataFrame(df, geometry="geometry", crs=reader(paths[0]).crs)
        df.to_parquet(merged / f"{layer}.parquet", index=False)
        print(f"  merged {layer}: {len(df):,} rows")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--huc4", nargs="+", default=[], help="HUC4 codes, e.g. 0710 1710")
    ap.add_argument("--huc2", nargs="+", default=[], help="all HUC4s in these HUC2 regions, e.g. 07")
    ap.add_argument("--list", action="store_true", help="list available HUC4s and exit")
    ap.add_argument("--out", type=Path, default=Path("raw/nhdplus_hr"), help="output root")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("MINX", "MINY", "MAXX", "MAXY"),
                    help="clip to lon/lat box (EPSG:4269/4326 degrees)")
    ap.add_argument("--mask", type=Path, help="clip to the union of polygons in this vector file")
    ap.add_argument("--merge", action="store_true", help="also write merged/<layer>.parquet")
    ap.add_argument("--download-only", action="store_true", help="download zips, skip extraction")
    ap.add_argument("--delete-zip", action="store_true", help="delete each zip after extraction")
    ap.add_argument("--delete-gdb", action="store_true", help="delete each .gdb after extraction")
    ap.add_argument("--force", action="store_true", help="re-extract layers even if outputs exist")
    args = ap.parse_args()
    warnings.filterwarnings("ignore", message="Measured \\(M\\) geometry")
    warnings.filterwarnings("ignore", message="organizePolygons")

    session = requests.Session()
    session.headers["User-Agent"] = "nhdplus-hr-downloader/1.0"

    available = list_available(session)
    if args.list:
        for h, size in available.items():
            print(f"{h}\t{size / 1e9:6.2f} GB")
        print(f"{len(available)} HUC4s, {sum(available.values()) / 1e12:.2f} TB total")
        return 0

    hucs = [h.zfill(4) for h in args.huc4]
    hucs += [h for h in available for r in args.huc2 if h.startswith(r.zfill(2))]
    hucs = sorted(set(hucs))
    if not hucs:
        ap.error("give --huc4 and/or --huc2 (or --list)")
    unknown = [h for h in hucs if h not in available]
    if unknown:
        ap.error(f"no NHDPlus HR GDB for HUC4 {unknown}; see --list")
    print(f"{len(hucs)} HUC4(s): {' '.join(hucs)}  "
          f"(~{sum(available[h] for h in hucs) / 1e9:.1f} GB of zips)")

    mask = None
    if args.mask:
        import geopandas as gpd
        mask = gpd.read_file(args.mask)
    bbox = tuple(args.bbox) if args.bbox else (
        tuple(mask.to_crs(4269).total_bounds) if mask is not None else None)

    zips = args.out / "_zips"
    gdbs = args.out / "_gdb"
    for h in hucs:
        print(f"[{h}]")
        layer_dir = args.out / h
        if not args.force and all((layer_dir / f"{l}.parquet").exists() for l in LAYERS):
            print("  layers already extracted (use --force to redo)")
            continue
        zp = download(session, zip_url(h), zips / f"NHDPLUS_H_{h}_HU4_GDB.zip")
        if args.download_only:
            continue
        gdb = extract_gdb(zp, gdbs / h)
        extract_layers(gdb, layer_dir, h, bbox=bbox, mask=mask)
        if args.delete_gdb:
            shutil.rmtree(gdbs / h)
        if args.delete_zip:
            zp.unlink()

    if args.merge and not args.download_only:
        merge_outputs(args.out, hucs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
