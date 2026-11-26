"""Read the rainfall grids (Stage IV daily, Atlas 14) onto a lon/lat grid and sample them at points."""

from __future__ import annotations

import gzip
import shutil
import tarfile
import tempfile
from datetime import date
from pathlib import Path

import numpy as np


def to_grid(src_path, band, bbox, res, scale=1.0, valid=lambda a: np.isfinite(a)):
    """Resample one raster band onto an EPSG:4326 grid over bbox (W, S, E, N), nearest neighbour.

    Returns (array, affine transform); cells outside the source or failing `valid` are NaN.
    """
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    w, s, e, n = bbox
    width, height = int(np.ceil((e - w) / res)), int(np.ceil((n - s) / res))
    dst_t = from_origin(w, n, res, res)
    dst = np.full((height, width), np.nan, dtype="float32")
    with rasterio.open(src_path) as src:
        a = src.read(band).astype("float32")
        a[~valid(a)] = np.nan
        reproject(a, dst, src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                  dst_transform=dst_t, dst_crs="EPSG:4326", dst_nodata=np.nan,
                  resampling=Resampling.nearest)
    return dst * scale, dst_t


def sample(grid, transform, xs, ys):
    """Grid value at lon/lat points (NaN outside the grid)."""
    from rasterio.transform import rowcol
    r, c = rowcol(transform, np.asarray(xs), np.asarray(ys))
    r, c = np.asarray(r), np.asarray(c)
    ok = (r >= 0) & (r < grid.shape[0]) & (c >= 0) & (c < grid.shape[1])
    out = np.full(len(r), np.nan)
    out[ok] = grid[r[ok], c[ok]]
    return out


def stage4_day(raw: Path, day: date, bbox, area: str = "conus", res: float = 0.02):
    """Observed 24-h rainfall (inches, 24 h ending 12:00 UTC on `day`) on a lon/lat grid.

    Reads the daily GeoTIFF (band 1) or, for days that only have the NCEP GRIB archive
    (2016, 2019, 2020 ...), the <area>_YYYYMMDD_24h.grb.gz inside the tar (mm -> in).
    Returns (grid, transform, source) or None when the day is not on disk.
    """
    folder = Path(raw) / "stage4" / f"{day:%Y}" / f"{day:%Y%m%d}"
    tif = folder / f"nws_precip_1day_{day:%Y%m%d}_{area}.tif"
    tar = folder / f"ncep_stage_iv_source_files_{day:%Y%m%d}.tar"
    if tif.exists():
        g, t = to_grid(tif, 1, bbox, res, valid=lambda a: np.isfinite(a) & (a >= 0))
        return g, t, "tif"
    if tar.exists():
        tmp = Path(tempfile.mkdtemp())
        try:
            with tarfile.open(tar) as tf:
                m = next((m for m in tf.getmembers() if m.name.endswith(f"{area}_{day:%Y%m%d}_24h.grb.gz")), None)
                if m is None:
                    return None
                with tf.extractfile(m) as fh, open(tmp / "x.grb", "wb") as out:
                    shutil.copyfileobj(gzip.GzipFile(fileobj=fh), out)
            g, t = to_grid(tmp / "x.grb", 1, bbox, res, scale=1 / 25.4,
                           valid=lambda a: np.isfinite(a) & (a >= 0) & (a < 9999))
            return g, t, "grib"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


def atlas14_grid(raw: Path, bbox, res: float = 0.01, pattern: str = "*_in.tif"):
    """Atlas 14 depth (inches) on a lon/lat grid, merging every region grid that covers bbox.

    Returns (grid, transform, [file names]) or None.
    """
    import rasterio
    from rasterio.warp import transform_bounds

    folder = Path(raw) / "atlas14"
    tifs = sorted(folder.glob(pattern)) if folder.exists() else []
    grid, tr, used = None, None, []
    for t in tifs:
        with rasterio.open(t) as r:
            b = transform_bounds(r.crs, "EPSG:4326", *r.bounds)
        if b[0] < bbox[2] and b[2] > bbox[0] and b[1] < bbox[3] and b[3] > bbox[1]:
            g, tr = to_grid(t, 1, bbox, res)
            grid = g if grid is None else np.where(np.isnan(grid), g, grid)
            used.append(t.name)
    return None if grid is None else (grid, tr, used)
