"""Shared helpers: HTTP session, resumable downloads, zip extraction, output."""

from __future__ import annotations

import json
import shutil
import time
import warnings
import zipfile
from pathlib import Path

import requests
from tqdm import tqdm

USER_AGENT = "kgdata-downloader/1.0 (research data pipeline)"

warnings.filterwarnings("ignore", message="Measured \\(M\\) geometry")
warnings.filterwarnings("ignore", message="organizePolygons")


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def log(msg: str) -> None:
    print(msg, flush=True)


def get(sess: requests.Session, url: str, retries: int = 6, timeout: int = 300,
        method: str = "GET", **kw) -> requests.Response:
    """GET (or another method) with exponential-backoff retries on network errors and 429/5xx."""
    last = None
    for attempt in range(retries):
        try:
            r = sess.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last = exc
            if isinstance(exc, requests.HTTPError) and exc.response is not None \
                    and exc.response.status_code in (400, 401, 403, 404):
                raise
            wait = min(5 * 2 ** attempt, 300)
            log(f"    retry {attempt + 1}/{retries} in {wait}s ({exc})")
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last}")


def download(sess: requests.Session, url: str, dest: Path, retries: int = 8,
             params: dict | None = None, resume: bool = True) -> Path:
    """Stream url to dest. Resumes a .part file when the server supports Range.

    Skips the download when dest already exists (delete it to force a refresh).
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  have {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(retries):
        have = part.stat().st_size if (resume and part.exists()) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with sess.get(url, params=params, headers=headers, stream=True, timeout=300,
                          allow_redirects=True) as r:
                if r.status_code == 416:  # already complete
                    break
                r.raise_for_status()
                if have and r.status_code != 206:
                    have = 0
                total = r.headers.get("Content-Length")
                total = int(total) + have if total else None
                with open(part, "ab" if have else "wb") as f, tqdm(
                    total=total, initial=have, unit="B", unit_scale=True,
                    desc=f"  {dest.name}", leave=False, mininterval=2,
                ) as bar:
                    for block in r.iter_content(1 << 20):
                        f.write(block)
                        bar.update(len(block))
                if total and part.stat().st_size < total:
                    raise IOError(f"incomplete {part.stat().st_size}/{total}")
            break
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, requests.HTTPError) and exc.response is not None \
                    and exc.response.status_code in (400, 401, 403, 404):
                raise
            wait = min(5 * 2 ** attempt, 300)
            log(f"    download error ({exc}); retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    else:
        raise RuntimeError(f"failed to download {url}")
    part.rename(dest)
    log(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def extract_zip(zip_path: Path, dest_dir: Path, members: list[str] | None = None) -> Path:
    """Extract (optionally selected members of) a zip, refusing paths outside dest_dir."""
    dest_dir = Path(dest_dir)
    marker = dest_dir / f".extracted_{Path(zip_path).name}"
    if marker.exists():
        return dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        names = members or zf.namelist()
        for n in names:
            if not str((dest_dir / n).resolve()).startswith(str(root)):
                raise RuntimeError(f"unsafe path in zip: {n}")
        zf.extractall(dest_dir, members=names)
    marker.touch()
    return dest_dir


def find_gdb(folder: Path) -> Path:
    gdbs = sorted(Path(folder).rglob("*.gdb"))
    if not gdbs:
        raise FileNotFoundError(f"no .gdb under {folder}")
    return gdbs[0]


def write_gdf(gdf, path: Path) -> None:
    """Write a (Geo)DataFrame to Parquet, forcing 2D geometry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(gdf, "geometry") and getattr(gdf, "_geometry_column_name", None) in gdf:
        gdf = gdf.set_geometry(gdf.geometry.force_2d())
    gdf.to_parquet(path, index=False)
    log(f"  wrote {path} ({len(gdf):,} rows)")


def rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def arcgis_layer(sess: requests.Session, layer_url: str, where: str = "1=1", batch: int = 500):
    """Read every feature of an ArcGIS REST map/feature layer into a GeoDataFrame (EPSG:4326).

    Object IDs are fetched first, then features in batches, so the server's
    maxRecordCount never truncates the result. Esri date fields (epoch ms) become datetimes.
    """
    import geopandas as gpd
    import pandas as pd

    meta = get(sess, layer_url, params={"f": "json"}).json()
    ids = get(sess, layer_url + "/query",
              params={"where": where, "returnIdsOnly": "true", "f": "json"}).json()
    if "error" in ids:
        raise RuntimeError(f"{layer_url}: {ids['error']}")
    oid_field, oids = ids["objectIdFieldName"], sorted(ids.get("objectIds") or [])
    feats = []
    for i in range(0, len(oids), batch):
        chunk = oids[i:i + batch]
        js = get(sess, layer_url + "/query", method="POST", data={  # POST: long id lists
            "objectIds": ",".join(map(str, chunk)), "outFields": "*", "outSR": 4326,
            "returnGeometry": "true", "f": "geojson"}).json()
        if "error" in js:
            raise RuntimeError(f"{layer_url}: {js['error']}")
        feats.extend(js["features"])
    gdf = gpd.GeoDataFrame.from_features(feats, crs=4326) if feats else gpd.GeoDataFrame(geometry=[], crs=4326)
    for f in meta.get("fields") or []:
        if f["type"] == "esriFieldTypeDate" and f["name"] in gdf:
            gdf[f["name"]] = pd.to_datetime(gdf[f["name"]], unit="ms", errors="coerce")
    if len(gdf) != len(oids):
        log(f"    warning: {len(oids)} ids but {len(gdf)} features from {layer_url}")
    gdf.attrs["source"] = layer_url
    gdf.attrs["object_id_field"] = oid_field
    return gdf


def flag_bbox(gdf, bbox: tuple[float, float, float, float]):
    """Add coord_ok: True when the point lies inside bbox (lon_min, lat_min, lon_max, lat_max).

    Source records with swapped lat/lon, a missing minus sign or a 0/90 placeholder get
    False; they are kept, not corrected.
    """
    pts = gdf.geometry.representative_point()
    gdf["coord_ok"] = pts.x.between(bbox[0], bbox[2]) & pts.y.between(bbox[1], bbox[3])
    bad = int((~gdf["coord_ok"]).sum())
    if bad:
        log(f"    {bad} records with coordinates outside {bbox} (coord_ok = False)")
    return gdf


def layer_to_parquet(src, layer: str, dest: Path, columns=None, batch: int = 20_000,
                     extra: dict | None = None) -> int:
    """Copy one vector layer to GeoParquet in batches, so memory stays small for huge layers.

    The result is the same as reading the whole layer with geopandas and calling
    to_parquet (2D WKB geometry, GeoParquet metadata, layer CRS), and is read back with
    geopandas.read_parquet. `extra` adds constant columns, e.g. {"state": "NC"}.
    Returns the number of features written.
    """
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyogrio
    import shapely

    info = pyogrio.read_info(src, layer=layer)
    total = int(info["features"])
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    writer, schema, n, crs = None, None, 0, None
    try:
        for start in range(0, max(total, 1), batch):
            gdf = pyogrio.read_dataframe(src, layer=layer, columns=columns, skip_features=start,
                                         max_features=batch, use_arrow=False)
            if crs is None:
                crs = gdf.crs
            if not len(gdf):
                if writer is not None:
                    continue
            attrs = gdf.drop(columns=gdf.geometry.name)
            for k, v in (extra or {}).items():
                attrs[k] = v
            if schema is None:  # fixed from the first batch; text columns stay text when all-null
                fields = []
                for c, dt in attrs.dtypes.items():
                    if dt == object or pd.api.types.is_string_dtype(dt):  # object or pandas 3 str
                        typ = pa.string()
                    elif str(dt).startswith("datetime64"):
                        typ = pa.timestamp("ms")
                    else:
                        typ = pa.from_numpy_dtype(dt)
                    fields.append(pa.field(c, typ))
                fields.append(pa.field("geometry", pa.binary()))
                geo = {"version": "1.0.0", "primary_column": "geometry",
                       "columns": {"geometry": {"encoding": "WKB", "geometry_types": [],
                                                "crs": crs.to_json_dict() if crs else None}}}
                schema = pa.schema(fields, metadata={b"geo": json.dumps(geo).encode()})
                writer = pq.ParquetWriter(part, schema, compression="snappy")
            wkb = shapely.to_wkb(shapely.force_2d(gdf.geometry.values), flavor="iso")
            cols = [pa.array(attrs[f.name].astype(object).where(attrs[f.name].notna(), None)
                             if pa.types.is_string(f.type) else attrs[f.name], type=f.type, from_pandas=True)
                    for f in schema if f.name != "geometry"]
            cols.append(pa.array(wkb, type=pa.binary()))
            writer.write_table(pa.Table.from_arrays(cols, schema=schema))
            n += len(gdf)
            if total > batch:
                log(f"    {min(start + batch, total):,} / {total:,} features")
    finally:
        if writer is not None:
            writer.close()
    part.replace(dest)
    log(f"  wrote {dest} ({n:,} rows)")
    return n
