"""Fetch bathymetry from the NOAA NCEI best-available DEM mosaic.

The raster is cut to the bounding box of the data being processed, so no fixed
survey area is baked into the pipeline.
"""

import sys

import rasterio
import requests

IMAGE_SERVER = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/"
                "DEM_all/ImageServer/exportImage")
MAX_PIXELS = 80_000_000


def grid_size(bounds, arcsec):
    west, south, east, north = bounds
    cols = round((east - west) * 3600 / arcsec)
    rows = round((north - south) * 3600 / arcsec)
    if cols < 2 or rows < 2:
        sys.exit(f"Requested area is too small for {arcsec} arc-second sampling: {cols}x{rows} pixels")
    if cols * rows > MAX_PIXELS:
        sys.exit(f"Requested grid is {cols}x{rows} = {cols * rows / 1e6:.0f} Mpixel, above the "
                 f"{MAX_PIXELS / 1e6:.0f} Mpixel limit. Use a coarser --arcsec or supply --dem.")
    return cols, rows


def covers(path, bounds, arcsec):
    """True when a cached raster spans bounds at the requested resolution."""
    if not path.exists():
        return False
    with rasterio.open(path) as src:
        b = src.bounds
        res_ok = abs(src.res[0] * 3600 - arcsec) < arcsec * 0.01
    # The server snaps the requested box to its own grid, so allow a pixel of slack.
    slack = arcsec / 3600
    return res_ok and (b.left <= bounds[0] + slack and b.bottom <= bounds[1] + slack
                       and b.right >= bounds[2] - slack and b.top >= bounds[3] - slack)


def fetch_dem(bounds, arcsec, path, refresh=False):
    """Download the DEM subset covering bounds, reusing a suitable cached file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not refresh and covers(path, bounds, arcsec):
        print(f"Using cached DEM {path}")
        return path

    cols, rows = grid_size(bounds, arcsec)
    print(f"Fetching {arcsec}\u2033 DEM {cols}x{rows} for "
          f"{bounds[1]:.3f}..{bounds[3]:.3f}N {bounds[0]:.3f}..{bounds[2]:.3f}E")
    response = requests.get(IMAGE_SERVER, params={
        "bbox": ",".join(f"{v:.6f}" for v in bounds),
        "bboxSR": 4326, "imageSR": 4326,
        "size": f"{cols},{rows}",
        "format": "tiff", "pixelType": "F32",
        "interpolation": "RSP_NearestNeighbor", "f": "image",
    }, timeout=900, stream=True)
    response.raise_for_status()
    if "image/tiff" not in response.headers.get("Content-Type", ""):
        sys.exit(f"NCEI returned {response.headers.get('Content-Type')}: {response.text[:400]}")

    with open(path, "wb") as fh:
        for chunk in response.iter_content(1 << 20):
            fh.write(chunk)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path


def data_bounds(lat, lon, pad_deg):
    return (float(lon.min()) - pad_deg, float(lat.min()) - pad_deg,
            float(lon.max()) + pad_deg, float(lat.max()) + pad_deg)
