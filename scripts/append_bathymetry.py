"""Append seabed depth and tide-corrected water depth to a vehicle export CSV.

Depth source: NOAA NCEI DEM mosaic GeoTIFF (CUDEM 1/9 arc-second tiles in this
area), elevation in metres relative to NAVD88, sampled bilinearly.

Tide: NOAA CO-OPS predicted water level (MLLW) at the chosen station, shifted to
NAVD88 with a VDatum offset computed at the track centroid.

  bathymetry_depth_m   = -seabed_elev_navd88
  tide_adjusted_depth_m = water_level_navd88(t) - seabed_elev_navd88
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import requests

from ncei_dem import data_bounds, fetch_dem

ROOT = Path(__file__).resolve().parents[1]


def slug(callsign):
    return callsign.strip().lower().replace(" ", "-")

COOPS_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
COOPS_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
NCEI_IDENTIFY_URL = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/identify"
VDATUM_URL = "https://vdatum.noaa.gov/vdatumweb/api/convert"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--callsign", required=True, help="Vehicle callsign, e.g. warden-2")
    p.add_argument("--csv", default=None, help="Input CSV (default data/raw/<callsign>_export.csv)")
    p.add_argument("--dem", default=None,
                   help="DEM GeoTIFF; by default one is fetched for the extent of the data")
    p.add_argument("--arcsec", type=float, default=1 / 3,
                   help="Resolution of the fetched DEM in arc-seconds (default 1/3, ~10 m)")
    p.add_argument("--pad-deg", type=float, default=0.02,
                   help="Margin added around the data extent when fetching the DEM")
    p.add_argument("--refresh-dem", action="store_true", help="Re-download even if a cached DEM covers the area")
    p.add_argument("--out", default=None,
                   help="Output CSV (default data/processed/<callsign>_export_with_depth.csv)")
    p.add_argument("--station", default=None,
                   help="NOAA CO-OPS station override (default: nearest water-level station)")
    args = p.parse_args()
    name = slug(args.callsign)
    if args.csv is None:
        args.csv = ROOT / f"data/raw/{name}_export.csv"
    if args.out is None:
        args.out = ROOT / f"data/processed/{name}_export_with_depth.csv"
    return args


def sample_bilinear(dem_path, lat, lon):
    with rasterio.open(dem_path) as src:
        band = src.read(1).astype("float64")
        inv = ~src.transform
        cols, rows = inv * (lon, lat)

    # Pixel centres sit half a pixel in from the sample grid corners.
    cols -= 0.5
    rows -= 0.5
    c0 = np.floor(cols).astype(int)
    r0 = np.floor(rows).astype(int)
    inside = (c0 >= 0) & (r0 >= 0) & (c0 < band.shape[1] - 1) & (r0 < band.shape[0] - 1)
    if not inside.all():
        sys.exit(f"{(~inside).sum()} positions fall outside the DEM extent")

    fc = cols - c0
    fr = rows - r0
    v00 = band[r0, c0]
    v01 = band[r0, c0 + 1]
    v10 = band[r0 + 1, c0]
    v11 = band[r0 + 1, c0 + 1]
    top = v00 * (1 - fc) + v01 * fc
    bottom = v10 * (1 - fc) + v11 * fc
    return top * (1 - fr) + bottom * fr


def mllw_to_navd88(lat, lon):
    r = requests.get(VDATUM_URL, params={
        "s_x": lon, "s_y": lat, "s_z": 0,
        "region": "contiguous", "s_coor": "geo",
        "s_h_frame": "NAD83_2011", "s_v_frame": "MLLW", "s_v_unit": "m",
        "t_h_frame": "NAD83_2011", "t_v_frame": "NAVD88", "t_v_unit": "m",
    }, timeout=60)
    r.raise_for_status()
    payload = r.json()
    offset = float(payload["t_z"])
    if offset <= -100:
        sys.exit(f"VDatum returned no conversion at {lat},{lon}: {payload}")
    return offset, float(payload.get("uncertainty", "nan"))


def great_circle_distance_m(lat_a, lon_a, lat_b, lon_b):
    radius_m = 6_371_000.0
    lat_delta = math.radians(lat_b - lat_a)
    lon_delta = math.radians(lon_b - lon_a)
    value = (math.sin(lat_delta / 2) ** 2
             + math.cos(math.radians(lat_a)) * math.cos(math.radians(lat_b))
             * math.sin(lon_delta / 2) ** 2)
    return 2 * radius_m * math.asin(math.sqrt(value))


def nearest_water_level_station(lat, lon):
    response = requests.get(COOPS_STATIONS_URL, params={"type": "waterlevels"}, timeout=60)
    response.raise_for_status()
    nearest = None
    nearest_distance_m = None
    for station in response.json().get("stations", []):
        try:
            distance_m = great_circle_distance_m(lat, lon, float(station["lat"]), float(station["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        if nearest_distance_m is None or distance_m < nearest_distance_m:
            nearest = station
            nearest_distance_m = distance_m
    if nearest is None:
        raise RuntimeError("CO-OPS station metadata contained no usable water-level stations")
    return str(nearest["id"]), str(nearest.get("name", "unknown")), nearest_distance_m


def tide_predictions(station, start, stop):
    r = requests.get(COOPS_URL, params={
        "product": "predictions", "application": "do-survey",
        "begin_date": start.strftime("%Y%m%d %H:%M"),
        "end_date": stop.strftime("%Y%m%d %H:%M"),
        "datum": "MLLW", "station": station, "time_zone": "gmt",
        "units": "metric", "format": "json",
    }, timeout=120)
    r.raise_for_status()
    payload = r.json()
    if "predictions" not in payload:
        sys.exit(f"CO-OPS error: {payload}")
    tide = pd.DataFrame(payload["predictions"])
    tide["t"] = pd.to_datetime(tide["t"], utc=True)
    tide["v"] = tide["v"].astype(float)
    return tide.set_index("t")["v"].sort_index()


def tide_adjusted_bathymetry_depth_m(lat, lon, timestamp):
    response = requests.get(NCEI_IDENTIFY_URL, params={
        "geometry": f'{{"x":{lon},"y":{lat},"spatialReference":{{"wkid":4326}}}}',
        "geometryType": "esriGeometryPoint",
        "returnGeometry": "false",
        "returnCatalogItems": "false",
        "f": "json",
    }, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error") or payload.get("value") is None:
        raise RuntimeError(f"NCEI identify error: {payload}")
    elevation_m = float(payload["value"])
    offset_m, _ = mllw_to_navd88(lat, lon)
    station, _, _ = nearest_water_level_station(lat, lon)
    tide_mllw = tide_predictions(station, timestamp - pd.Timedelta(hours=1), timestamp + pd.Timedelta(hours=1))
    tide_m = np.interp(timestamp.value, tide_mllw.index.astype("int64"), tide_mllw.to_numpy())
    return tide_m + offset_m - elevation_m


def main():
    args = parse_args()
    df = pd.read_csv(args.csv, parse_dates=["timestamp"])

    lat = df["global_position_int.lat"] / 1e7
    lon = df["global_position_int.lon"] / 1e7
    fix = lat.notna() & lon.notna()
    print(f"{fix.sum():,} rows with a position")

    dem = args.dem
    if dem is None:
        bounds = data_bounds(lat[fix], lon[fix], args.pad_deg)
        dem = fetch_dem(bounds, args.arcsec,
                        ROOT / f"data/bathymetry/{slug(args.callsign)}_dem_{args.arcsec:.3f}arcsec.tif",
                        refresh=args.refresh_dem)

    elev = np.full(len(df), np.nan)
    elev[fix.to_numpy()] = sample_bilinear(dem, lat[fix].to_numpy(), lon[fix].to_numpy())

    centroid_lat, centroid_lon = lat[fix].mean(), lon[fix].mean()
    offset, uncertainty = mllw_to_navd88(centroid_lat, centroid_lon)
    print(f"VDatum MLLW->NAVD88 at {centroid_lat:.4f},{centroid_lon:.4f}: {offset:+.3f} m (±{uncertainty:.3f})")

    if args.station:
        station, station_name = args.station, "user override"
    else:
        station, station_name, station_distance_m = nearest_water_level_station(centroid_lat, centroid_lon)
        print(f"Nearest tide station: {station} {station_name} ({station_distance_m / 1000:.1f} km)")
    start, stop = df["timestamp"].min(), df["timestamp"].max()
    tide_mllw = tide_predictions(station, start - pd.Timedelta(hours=1), stop + pd.Timedelta(hours=1))
    print(f"Tide station {station} ({station_name}): {len(tide_mllw):,} predictions "
          f"{tide_mllw.min():.2f}-{tide_mllw.max():.2f} m MLLW")

    water_level = np.interp(
        df["timestamp"].astype("int64").to_numpy(),
        tide_mllw.index.astype("int64").to_numpy(),
        tide_mllw.to_numpy(),
    ) + offset

    df["bathymetry_depth_m"] = -elev
    df["tide_adjusted_depth_m"] = water_level - elev
    df.loc[~fix, "tide_adjusted_depth_m"] = np.nan
    df["tide_station"] = station
    df["vdatum_offset_m"] = offset

    df.to_csv(args.out, index=False)
    valid = df["bathymetry_depth_m"].notna()
    print(f"Wrote {args.out} ({len(df):,} rows)")
    print(f"  bathymetry_depth_m   {df.loc[valid, 'bathymetry_depth_m'].min():.2f} to "
          f"{df.loc[valid, 'bathymetry_depth_m'].max():.2f} m")
    print(f"  tide_adjusted_depth_m {df.loc[valid, 'tide_adjusted_depth_m'].min():.2f} to "
          f"{df.loc[valid, 'tide_adjusted_depth_m'].max():.2f} m")


if __name__ == "__main__":
    main()
