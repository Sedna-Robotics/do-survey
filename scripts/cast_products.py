"""Build the derived cast dataset and the PDF / interactive map products.

Reads the depth-augmented export (left untouched) and writes:
  <callsign>_casts_derived.csv  one row per DO reading, with on-bottom tagging
  <callsign>_casts.pdf          per-cast profile plots, a map, and a methods page
  <callsign>_casts_map.html     Leaflet map, click a station for its inset plot
"""

import argparse
import html
import json
import os
import re
import textwrap
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import rasterio

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LightSource
from matplotlib.lines import Line2D

from ncei_dem import data_bounds, fetch_dem

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT / "outputs/assets/sedna_logo_transparent.png"


def slug(callsign):
    return callsign.strip().lower().replace(" ", "-")

# Legend from the supplied scale: lower bound -> colour.
DO_BINS = [(6.0, "#5b9bd5", "6 mg/L+"), (4.0, "#d9cf3f", "4 mg/L+"),
           (2.0, "#e08b3e", "2 mg/L+"), (0.0, "#e05561", "0 mg/L+")]
DO_BELOW_ZERO = "#000000"
TAGGED_COLOR = "#b0b0b0"
SPEED_THRESHOLD = 0.3
MIN_LINE_LENGTH = 1.5
TIDE_STATION = "8447241 Sesuit Harbor, East Dennis (41.7517°N, 70.1550°W)"
VDATUM_OFFSET_M = -1.764
VDATUM_UNCERTAINTY_M = 0.117


def provenance():
    return [
        ("Data sources", [
            "Vehicle telemetry: InfluxDB Cloud bucket <code>"
            + os.environ.get("INFLUX_BUCKET", "warden")
            + "</code>, callsign <code>__CALLSIGN__</code>, "
            "measurements <code>global_position_int</code>, <code>winch_status</code>, <code>profile_sample</code>.",
            "Bathymetry: NOAA NCEI best-available DEM mosaic (<code>DEM_mosaics/DEM_all</code> ImageServer). "
            "In this area the mosaic is fed by the CUDEM 1/9 arc-second tiles "
            "<code>ncei19_n41x75/n42x00_w070x25/w070x50_2021v1</code>, exported at 1/3 arc-second (~10 m).",
            f"Tides: NOAA CO-OPS predictions, station {TIDE_STATION}, datum MLLW, metric, 6-minute interval.",
            f"Vertical datum transform: NOAA VDatum, MLLW→NAVD88 = {VDATUM_OFFSET_M:+.3f} m "
            f"(±{VDATUM_UNCERTAINTY_M:.3f} m) evaluated at the track centroid.",
        ]),
        ("Filtering and processing", [
            "<code>global_position_int</code> and <code>winch_status</code> reduced to 1 Hz server-side with "
            "<code>aggregateWindow(every: 1s, fn: last, createEmpty: false)</code>; "
            "<code>profile_sample</code> kept at its native 30 s rate.",
            "Winch line length and speed are linearly interpolated in time onto each DO sample timestamp; "
            "gaps longer than 30 s are left null.",
            f"A reading is tagged <b>not on bottom</b> when |winch speed| &gt; {SPEED_THRESHOLD} m/s "
            f"or line length &lt; {MIN_LINE_LENGTH} m. Tagged readings are greyed out and excluded from "
            "the station minimum shown on the map.",
            "Seabed elevation is sampled bilinearly from the DEM at each vessel fix.",
        ]),
        ("Assumptions and caveats", [
            "DEM elevations are treated as metres relative to NAVD88, the CUDEM vertical datum. "
            "<code>bathymetry_depth_m</code> is the negated seabed elevation; "
            "<code>tide_adjusted_depth_m</code> adds the predicted water level.",
            "Tides are <i>predicted</i> astronomical levels, not observed water levels: storm surge and "
            "wind setup are not represented.",
            "One tide station and one VDatum offset are applied across the whole survey area, so spatial "
            "variation in tidal phase, range and datum separation is ignored. Sesuit Harbor is a "
            "subordinate station, derived from a reference station rather than measured harmonics.",
            "Winch line length is used as the depth proxy for each reading. There is no wire angle or "
            "layback correction, so a reading's true depth is at most its line length.",
            "The Lowell Instruments DOT-2 logger does not report pressure "
            "(<code>profile_sample.pressure_dbar</code> is zero throughout), so no independent sensor "
            "depth is available as a check.",
            "Positions are the vessel GPS fix (<code>global_position_int</code>), not the position of the "
            "sensor itself. The vessel free-drifts during sensor deployment.",
            "Quoted uncertainty covers the VDatum transform only. DEM vertical uncertainty, tide prediction "
            "error and GPS error are not included.",
            "Where every reading at a station is tagged, no minimum on-bottom DO exists and the station is "
            "drawn grey.",
        ]),
    ]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--callsign", required=True, help="Vehicle callsign, e.g. warden-2")
    p.add_argument("--csv", default=None,
                   help="Input CSV (default data/processed/<callsign>_export_with_depth.csv)")
    p.add_argument("--dem", default=None,
                   help="DEM GeoTIFF for the map background; by default one is fetched for the route extent")
    p.add_argument("--arcsec", type=float, default=3.0,
                   help="Resolution of the fetched map-background DEM in arc-seconds")
    p.add_argument("--pad-deg", type=float, default=0.02,
                   help="Margin added around the route extent for the map")
    p.add_argument("--refresh-dem", action="store_true", help="Re-download even if a cached DEM covers the area")
    p.add_argument("--location", default=None,
             help="General location label for product names (default auto from route centroid)")
    p.add_argument("--derived", default=None,
                   help="Output CSV (default data/processed/<callsign>_casts_derived.csv)")
    p.add_argument("--pdf", default=None, help="Output PDF (default outputs/<callsign>_<location>_<range>_casts.pdf)")
    p.add_argument("--html", default=None, help="Output HTML (default outputs/<callsign>_<location>_<range>_casts_map.html)")
    p.add_argument("--timezone", default="America/New_York", help="IANA zone used for displayed cast times")
    args = p.parse_args()
    name = slug(args.callsign)
    if args.csv is None:
        args.csv = ROOT / f"data/processed/{name}_export_with_depth.csv"
    if args.derived is None:
        args.derived = ROOT / f"data/processed/{name}_casts_derived.csv"
    return args


def slug_text(value):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return value.strip("-") or "unknown"


def auto_location_label(route):
    lat = float(route["lat"].mean())
    lon = float(route["lon"].mean())
    ns = "n" if lat >= 0 else "s"
    ew = "e" if lon >= 0 else "w"
    return f"{abs(lat):.2f}{ns}_{abs(lon):.2f}{ew}".replace(".", "p")


def product_base_name(callsign, route, timestamps, timezone, location_override=None):
    start = timestamps.min().tz_convert(timezone)
    stop = timestamps.max().tz_convert(timezone)
    date_range = f"{start:%Y%m%dT%H%M}-{stop:%Y%m%dT%H%M}"
    callsign_part = slug_text(callsign)
    location_part = slug_text(location_override) if location_override else auto_location_label(route)
    return f"{callsign_part}_{location_part}_{date_range}"


def do_color(value):
    if value is None or not np.isfinite(value):
        return TAGGED_COLOR
    for lower, color, _ in DO_BINS:
        if value >= lower:
            return color
    return DO_BELOW_ZERO


def build_derived(df):
    winch = df.loc[df["winch_status.line_length"].notna(),
                   ["timestamp", "winch_status.line_length", "winch_status.speed"]].sort_values("timestamp")
    samples = df[df["profile_sample.profile_id"].notna()].copy().sort_values("timestamp")

    # The 1 Hz winch series has occasional dropouts, so interpolate rather than
    # discard samples that land in a gap.
    wt = winch["timestamp"].astype("int64").to_numpy()
    st = samples["timestamp"].astype("int64").to_numpy()
    line_length = np.interp(st, wt, winch["winch_status.line_length"].to_numpy())
    speed = np.interp(st, wt, winch["winch_status.speed"].to_numpy())
    gap = np.abs(wt[np.searchsorted(wt, st).clip(0, len(wt) - 1)] - st) / 1e9
    line_length[gap > 30] = np.nan
    speed[gap > 30] = np.nan

    out = pd.DataFrame({
        "timestamp": samples["timestamp"].to_numpy(),
        "profile_id": samples["profile_sample.profile_id"].astype("int64").to_numpy(),
        "sample_index": samples["profile_sample.sample_index"].astype("int64").to_numpy(),
        "lat": samples["profile_sample.current_lat_deg"].to_numpy(),
        "lon": samples["profile_sample.current_lon_deg"].to_numpy(),
        "dissolved_oxygen_mg_l": samples["profile_sample.dissolved_oxygen_mg_l"].to_numpy(),
        "temperature_c": samples["profile_sample.temperature_c"].to_numpy(),
        "line_length_m": line_length,
        "winch_speed_ms": speed,
        "bathymetry_depth_m": samples["bathymetry_depth_m"].to_numpy(),
        "tide_adjusted_depth_m": samples["tide_adjusted_depth_m"].to_numpy(),
    })
    out["not_on_bottom"] = ((out["winch_speed_ms"].abs() > SPEED_THRESHOLD)
                            | (out["line_length_m"] < MIN_LINE_LENGTH))

    # Order casts chronologically and give them 1-based station numbers.
    order = out.groupby("profile_id")["timestamp"].min().sort_values()
    station = {pid: i + 1 for i, pid in enumerate(order.index)}
    out["station"] = out["profile_id"].map(station)
    return out.sort_values(["station", "sample_index"]).reset_index(drop=True)


def cast_records(derived, timezone):
    records = []
    for station, grp in derived.groupby("station"):
        on_bottom = grp[~grp["not_on_bottom"]]
        records.append({
            "station": int(station),
            "profile_id": int(grp["profile_id"].iloc[0]),
            "lat": float(grp["lat"].mean()),
            "lon": float(grp["lon"].mean()),
            "time": grp["timestamp"].min().tz_convert(timezone).strftime("%Y-%m-%d %H:%M %Z"),
            "bathy_raw": float(grp["bathymetry_depth_m"].mean()),
            "bathy_tide": float(grp["tide_adjusted_depth_m"].mean()),
            "min_on_bottom_do": float(on_bottom["dissolved_oxygen_mg_l"].min()) if len(on_bottom) else None,
            "samples": [
                {
                    "n": int(r.sample_index) + 1,
                    "do": float(r.dissolved_oxygen_mg_l),
                    "line": float(r.line_length_m) if np.isfinite(r.line_length_m) else None,
                    "speed": float(r.winch_speed_ms) if np.isfinite(r.winch_speed_ms) else None,
                    "tagged": bool(r.not_on_bottom),
                }
                for r in grp.itertuples()
            ],
        })
    return records


def draw_cast(ax, cast):
    samples = [s for s in cast["samples"] if s["line"] is not None]
    do = np.array([s["do"] for s in samples])
    line = np.array([s["line"] for s in samples])
    tagged = np.array([s["tagged"] for s in samples])
    colors = [TAGGED_COLOR if t else do_color(v) for v, t in zip(do, tagged)]

    ax.plot(do, line, "-", color="#999999", lw=0.8, zorder=1)
    ax.scatter(do, line, c=colors, s=90, edgecolors="#333333", linewidths=0.6, zorder=3)
    for s, x, y in zip(samples, do, line):
        ax.annotate(str(s["n"]), (x, y), fontsize=6, ha="center", va="center",
                    color="white" if not s["tagged"] else "#333333", zorder=4)

    ax.axhline(cast["bathy_raw"], color="#1f4e79", ls="--", lw=1,
               label=f"bathy (NAVD88) {cast['bathy_raw']:.1f} m")
    ax.axhline(cast["bathy_tide"], color="#1f4e79", ls=":", lw=1.2,
               label=f"tide-corrected {cast['bathy_tide']:.1f} m")

    ax.set_title(f"Station {cast['station']} — {cast['lat']:.4f}\u00b0N, {abs(cast['lon']):.4f}\u00b0W\n{cast['time']}",
                 fontsize=8)
    ax.set_xlabel("Dissolved oxygen (mg/L)", fontsize=7)
    ax.set_ylabel("Line length (m)", fontsize=7)
    ax.tick_params(labelsize=6)
    ax.invert_yaxis()
    ax.grid(alpha=0.25, lw=0.5)
    ax.legend(fontsize=5.5, loc="lower left", framealpha=0.9)


def load_dem(dem_path, bounds):
    west, south, east, north = bounds
    with rasterio.open(dem_path) as src:
        window = rasterio.windows.from_bounds(west, south, east, north, src.transform)
        band = src.read(1, window=window).astype("float64")
        extent = rasterio.windows.bounds(window, src.transform)
    return band, (extent[0], extent[2], extent[1], extent[3])


def draw_map(ax, dem, extent, route, casts, callsign):
    water = np.ma.masked_greater(dem, 0)
    land = np.ma.masked_less_equal(dem, 0)
    shade = LightSource(azdeg=315, altdeg=45).hillshade(-dem, vert_exag=40,
                                                        dx=extent[1] - extent[0], dy=extent[3] - extent[2])

    ax.imshow(water, extent=extent, origin="upper", cmap="Blues_r", vmin=-45, vmax=8, alpha=0.9)
    ax.imshow(land, extent=extent, origin="upper", cmap="Greys", vmin=-40, vmax=120, alpha=0.9)
    ax.imshow(shade, extent=extent, origin="upper", cmap="gray", alpha=0.22)

    ax.plot(route["lon"], route["lat"], "-", color="black", lw=0.8, zorder=3)
    for cast in casts:
        ax.plot(cast["lon"], cast["lat"], "o", markersize=11, zorder=4,
                color=do_color(cast["min_on_bottom_do"]), markeredgecolor="#222222", markeredgewidth=0.7)
        ax.annotate(str(cast["station"]), (cast["lon"], cast["lat"]), fontsize=6,
                    ha="center", va="center", color="white", zorder=5)

    ax.set_aspect(1 / np.cos(np.radians(np.mean(extent[2:]))))
    ax.set_xlabel("Longitude", fontsize=8)
    ax.set_ylabel("Latitude", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_title(f"{callsign} route and minimum on-bottom dissolved oxygen", fontsize=10)

    handles = [Line2D([], [], marker="o", ls="", color=c, markersize=7, label=lbl)
               for _, c, lbl in DO_BINS[::-1]]
    handles.append(Line2D([], [], color="black", lw=1, label="Route"))
    ax.legend(handles=handles, fontsize=6, loc="lower left", title="DO", title_fontsize=7, framealpha=0.9)


def draw_notes(fig, callsign):
    """Render the provenance list as a plain-text page."""
    fig.text(0.06, 0.95, "Sources, filtering and assumptions", fontsize=14, weight="bold")
    y = 0.90
    for title, items in provenance():
        fig.text(0.06, y, title.upper(), fontsize=9, weight="bold", color="#1f4e79")
        y -= 0.022
        for item in items:
            text = html.unescape(re.sub("<[^>]+>", "", item.replace("__CALLSIGN__", callsign)))
            lines = textwrap.wrap(text, width=118)
            fig.text(0.07, y, "\u2022", fontsize=8)
            for i, line in enumerate(lines):
                fig.text(0.085, y - i * 0.019, line, fontsize=8)
            y -= len(lines) * 0.019 + 0.008
        y -= 0.016


def add_pdf_brand_header(fig):
    """Add a small Sedna logo in the page header."""
    if not LOGO_PATH.exists():
      return

    logo = plt.imread(LOGO_PATH)
    logo_h, logo_w = logo.shape[:2]
    aspect = logo_w / logo_h

    fig_w, fig_h = fig.get_size_inches()
    width_frac = 0.08
    width_in = fig_w * width_frac
    height_in = width_in / aspect
    height_frac = height_in / fig_h

    left = 0.98 - width_frac
    bottom = 0.98 - height_frac
    ax_logo = fig.add_axes([left, bottom, width_frac, height_frac], zorder=20)
    ax_logo.imshow(logo)
    ax_logo.axis("off")


def write_outputs_index(path, products_dir, title="Sedna Survey Products"):
    output_dir = products_dir
    survey_pattern = re.compile(
      r"^(?P<callsign>[a-z0-9-]+)_(?P<location>[a-z0-9_-]+)_(?P<range>\d{8}T\d{4}-\d{8}T\d{4})_casts$"
    )

    def humanize_location(raw_location):
        if re.fullmatch(r"\d+p\d+[ns]_\d+p\d+[ew]", raw_location):
            lat_raw, lon_raw = raw_location.split("_")
            lat_val = lat_raw[:-1].replace("p", ".")
            lon_val = lon_raw[:-1].replace("p", ".")
            lat = f"{lat_val}°{'N' if lat_raw.endswith('n') else 'S'}"
            lon = f"{lon_val}°{'E' if lon_raw.endswith('e') else 'W'}"
            return f"{lat}, {lon}"
        return raw_location.replace("-", " ").title()

    def humanize_range(raw_range):
        start_raw, stop_raw = raw_range.split("-")
        start = pd.to_datetime(start_raw, format="%Y%m%dT%H%M")
        stop = pd.to_datetime(stop_raw, format="%Y%m%dT%H%M")
        if start.date() == stop.date():
            return f"{start:%Y-%m-%d %H:%M} to {stop:%H:%M}"
        return f"{start:%Y-%m-%d %H:%M} to {stop:%Y-%m-%d %H:%M}"

    surveys = {}
    for file_path in sorted(output_dir.iterdir()):
        if not file_path.is_file() or file_path.name == "index.html":
            continue
        if file_path.suffix not in {".html", ".pdf"}:
            continue

        stem = file_path.stem
        if stem.endswith("_map"):
            base_stem = stem[:-4]
            file_kind = "map"
        else:
            base_stem = stem
            file_kind = "pdf"

        entry = surveys.setdefault(base_stem, {"map": None, "pdf": None, "name": None})
        entry[file_kind] = f"outputs/{file_path.name}"

        match = survey_pattern.match(base_stem)
        if match:
            callsign = match.group("callsign").replace("-", " ").title()
            location = humanize_location(match.group("location"))
            date_range = humanize_range(match.group("range"))
            entry["name"] = f"{callsign} | {location} | {date_range}"
        elif entry["name"] is None:
            entry["name"] = base_stem.replace("_", " ").replace("-", " ").title()

    ordered = sorted(surveys.items(), key=lambda item: item[0], reverse=True)

    rows = "\n".join(
        "<li>"
        f'<div class="surveyName">{meta["name"]}</div>'
        '<div class="links">'
        + (f'<a href="{meta["map"]}">Map</a>' if meta["map"] else '<span class="missing">Map</span>')
        + (f'<a href="{meta["pdf"]}">PDF</a>' if meta["pdf"] else '<span class="missing">PDF</span>')
        + "</div>"
        "</li>"
        for _, meta in ordered
    ) or '<li><span>No products found yet.</span></li>'

    html_text = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{title}</title>
  <style>
    :root {{
      --sedna-deep: #0B2C3D;
      --sedna-teal: #1F5E6E;
      --sedna-gold: #D9A441;
      --panel: #ffffff;
      --ink: #16313f;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, sans-serif;
      background: radial-gradient(circle at top right, #1f5e6e 0%, #0b2c3d 55%, #081f2b 100%);
      color: #f2f6f8;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }}
    .card {{
      width: min(900px, 96vw);
      background: rgba(255,255,255,0.96);
      color: var(--ink);
      border-radius: 14px;
      box-shadow: 0 16px 40px rgba(0,0,0,.28);
      border: 1px solid rgba(11,44,61,.14);
      overflow: hidden;
    }}
    header {{
      border-bottom: 3px solid var(--sedna-gold);
      padding: 16px 20px;
      display: flex;
      align-items: center;
      gap: 16px;
      background: #f6fbfd;
    }}
    header img {{ width: 140px; height: auto; }}
    header h1 {{ margin: 0; font-size: 1.25rem; color: var(--sedna-deep); }}
    header p {{ margin: 4px 0 0; color: var(--sedna-teal); font-size: .92rem; }}
    main {{ padding: 18px 20px 20px; }}
    ul {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }}
    li {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #f9fcfe;
      border: 1px solid rgba(31,94,110,.18);
      border-radius: 8px;
      padding: 10px 12px;
    }}
    .surveyName {{
      font-weight: 600;
      color: var(--sedna-deep);
      padding-right: 12px;
      line-height: 1.3;
    }}
    .links {{
      display: inline-flex;
      gap: 10px;
      min-width: 110px;
      justify-content: flex-end;
      align-items: center;
      flex-shrink: 0;
    }}
    a {{ color: var(--sedna-deep); font-weight: 700; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    span {{ color: var(--sedna-teal); font-size: .86rem; }}
    .missing {{ color: #6f8c96; font-weight: 600; }}
  </style>
</head>
<body>
  <section class=\"card\">
    <header>
      <img src=\"outputs/assets/sedna_logo_transparent.png\" alt=\"Sedna Robotics logo\">
      <div>
        <h1>{title}</h1>
        <p>Survey maps and PDF reports</p>
      </div>
    </header>
    <main>
      <ul>
        {rows}
      </ul>
    </main>
  </section>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)


def write_pdf(path, casts, dem, extent, route, callsign):
    with PdfPages(path) as pdf:
        fig, axes = plt.subplots(2, 5, figsize=(16, 7.5))
        for ax, cast in zip(axes.ravel(), casts):
            draw_cast(ax, cast)
        for ax in axes.ravel()[len(casts):]:
            ax.axis("off")
        fig.suptitle(f"{callsign} dissolved oxygen casts — grey points tagged not-on-bottom "
                     f"(|winch speed| > {SPEED_THRESHOLD} m/s or line length < {MIN_LINE_LENGTH} m)", fontsize=11)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        add_pdf_brand_header(fig)
        pdf.savefig(fig)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(11, 8.5))
        draw_map(ax, dem, extent, route, casts, callsign)
        fig.tight_layout()
        add_pdf_brand_header(fig)
        pdf.savefig(fig)
        plt.close(fig)

        fig = plt.figure(figsize=(11, 8.5))
        draw_notes(fig, callsign)
        add_pdf_brand_header(fig)
        pdf.savefig(fig)
        plt.close(fig)


def route_geojson(route, step=15):
    thinned = route.iloc[::step]
    return [[round(float(lat), 5), round(float(lon), 5)] for lat, lon in zip(thinned["lat"], thinned["lon"])]


def write_html(path, casts, route, callsign):
    payload = {
        "casts": casts,
        "route": route_geojson(route),
        "bins": [[b[0], b[1], b[2]] for b in DO_BINS],
        "belowZero": DO_BELOW_ZERO,
        "tagged": TAGGED_COLOR,
        "threshold": SPEED_THRESHOLD,
        "provenance": [[title, [i.replace("__CALLSIGN__", callsign) for i in items]]
                       for title, items in provenance()],
    }
    html_text = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload)).replace("__CALLSIGN__", callsign)
    with open(path, "w") as fh:
        fh.write(html_text)


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>__CALLSIGN__ dissolved oxygen casts</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  :root {
    --sedna-deep: #0B2C3D;
    --sedna-teal: #1F5E6E;
    --sedna-gold: #D9A441;
    --panel-bg: rgba(255,255,255,.94);
  }
  html, body {
    margin: 0;
    height: 100%;
    font-family: system-ui, sans-serif;
    background: linear-gradient(140deg, #081f2b 0%, #0b2c3d 100%);
    color: #eef4f6;
  }
  #map { position: absolute; inset: 0; }
  #brandMark {
    position: absolute;
    left: 56px;
    top: 12px;
    z-index: 1100;
    background: var(--panel-bg);
    border: 1px solid rgba(11,44,61,.18);
    border-radius: 8px;
    box-shadow: 0 2px 10px rgba(0,0,0,.25);
    padding: 5px 8px;
  }
  #brandMark img {
    display: block;
    width: 110px;
    height: auto;
  }
  #inset { position: absolute; right: 12px; top: 12px; width: 430px; height: 430px;
           background: #ffffff; color: #222; border-radius: 6px; box-shadow: 0 2px 12px rgba(0,0,0,.5);
           display: none; z-index: 1000; }
  #inset .close { position: absolute; right: 8px; top: 4px; cursor: pointer; font-size: 18px; z-index: 1; }
  #chart { width: 100%; height: 100%; }
  .legend { background: var(--panel-bg); color: var(--sedna-deep); padding: 8px 10px; border-radius: 4px;
            font-size: 12px; line-height: 18px; }
  .legend i { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
              margin-right: 6px; vertical-align: -1px; }
  .stationLabel { font: 700 11px system-ui; color: #fff; text-align: center; text-shadow: 0 0 2px #000; }
  .infoBtn { width: 26px; height: 26px; border-radius: 4px; background: var(--panel-bg);
             color: var(--sedna-deep); font: 700 16px Georgia, serif; text-align: center; line-height: 26px;
             cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
  #about { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
           width: min(760px, 92vw); max-height: 84vh; overflow-y: auto; background: #fff; color: #222;
           border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,.6); padding: 18px 24px 24px;
           display: none; z-index: 1200; font-size: 13px; line-height: 1.5; }
  #about h2 { margin: 0 0 4px; font-size: 16px; }
  #about h3 { margin: 16px 0 4px; font-size: 13px; text-transform: uppercase;
              letter-spacing: .04em; color: var(--sedna-teal); }
  #about ul { margin: 0; padding-left: 18px; }
  #about li { margin-bottom: 5px; }
  #about code { background: #f0f0f0; padding: 0 3px; border-radius: 3px; font-size: 12px; }
  #about .close { position: absolute; right: 12px; top: 8px; cursor: pointer; font-size: 22px; }
  .unitCtrl { background: var(--panel-bg); color: var(--sedna-deep); padding: 6px 8px; border-radius: 4px;
              font-size: 12px; line-height: 1.2; box-shadow: 0 1px 4px rgba(0,0,0,.25); }
  .unitCtrl label { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; cursor: pointer; }
  .unitCtrl label:last-child { margin-right: 0; }
</style>
</head>
<body>
<div id="map"></div>
<div id="brandMark"><img src="assets/sedna_logo_transparent.png" alt="Sedna Robotics logo"></div>
<div id="inset"><span class="close" onclick="document.getElementById('inset').style.display='none'">&times;</span>
  <div id="chart"></div></div>
<div id="about"><span class="close" onclick="document.getElementById('about').style.display='none'">&times;</span>
  <h2>Sources, filtering and assumptions</h2><div id="aboutBody"></div></div>
<script>
const DATA = __PAYLOAD__;
const METERS_PER_FATHOM = 1.8288;
let lengthUnit = 'm';
let selectedCast = null;

function doColor(v) {
  if (v === null || v === undefined || isNaN(v)) return DATA.tagged;
  for (const [lower, color] of DATA.bins) if (v >= lower) return color;
  return DATA.belowZero;
}

function lengthLabel() {
  return lengthUnit === 'm' ? 'm' : 'fathoms';
}

function toDisplayLength(v) {
  if (v === null || v === undefined || isNaN(v)) return v;
  return lengthUnit === 'm' ? v : v / METERS_PER_FATHOM;
}

const map = L.map('map');
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 13, attribution: 'Esri World Ocean Base' }).addTo(map);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 13 }).addTo(map);

const route = L.polyline(DATA.route, { color: '#0B2C3D', weight: 2.5, opacity: 0.95 }).addTo(map);
map.fitBounds(route.getBounds().pad(0.15));

DATA.casts.forEach(cast => {
  const marker = L.circleMarker([cast.lat, cast.lon], {
    radius: 10, color: '#0B2C3D', weight: 1,
    fillColor: doColor(cast.min_on_bottom_do), fillOpacity: 1
  }).addTo(map);
  marker.bindTooltip(`Station ${cast.station}<br>min on-bottom DO: ` +
    (cast.min_on_bottom_do === null ? 'n/a' : cast.min_on_bottom_do.toFixed(2) + ' mg/L'));
  marker.on('click', () => showCast(cast));
  L.marker([cast.lat, cast.lon], { interactive: false,
    icon: L.divIcon({ className: 'stationLabel', html: cast.station, iconSize: [16, 16] }) }).addTo(map);
});

function showCast(cast) {
  selectedCast = cast;
  const pts = cast.samples.filter(s => s.line !== null);
  const yVals = pts.map(s => toDisplayLength(s.line));
  const bathyRaw = toDisplayLength(cast.bathy_raw);
  const bathyTide = toDisplayLength(cast.bathy_tide);
  const unit = lengthLabel();
  const traces = [{
    x: pts.map(s => s.do), y: yVals, mode: 'lines', line: { color: '#bbb', width: 1 },
    hoverinfo: 'skip', showlegend: false
  }, {
    x: pts.map(s => s.do), y: yVals, mode: 'markers+text',
    text: pts.map(s => s.n), textposition: 'middle center',
    textfont: { size: 8, color: '#fff' },
    marker: { size: 18, color: pts.map(s => s.tagged ? DATA.tagged : doColor(s.do)),
              line: { color: '#333', width: 1 } },
    customdata: pts.map(s => [s.speed, s.tagged ? 'not on bottom' : 'on bottom']),
    hovertemplate: '#%{text}<br>DO %{x:.2f} mg/L<br>line %{y:.2f} ' + unit +
                   '<br>winch %{customdata[0]:.2f} m/s (%{customdata[1]})<extra></extra>',
    showlegend: false
  }];
  const shapes = [
    { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: bathyRaw, y1: bathyRaw,
      line: { color: '#1f4e79', width: 1, dash: 'dash' } },
    { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: bathyTide, y1: bathyTide,
      line: { color: '#1f4e79', width: 1.5, dash: 'dot' } }
  ];
  const annotations = [
    { xref: 'paper', x: 0.02, y: bathyRaw, text: `bathy ${bathyRaw.toFixed(1)} ${unit}`,
      showarrow: false, font: { size: 9, color: '#1f4e79' }, yanchor: 'bottom', xanchor: 'left' },
    { xref: 'paper', x: 0.98, y: bathyTide, text: `tide-corrected ${bathyTide.toFixed(1)} ${unit}`,
      showarrow: false, font: { size: 9, color: '#1f4e79' }, yanchor: 'top', xanchor: 'right' }
  ];
  document.getElementById('inset').style.display = 'block';
  Plotly.newPlot('chart', traces, {
    title: { text: `Station ${cast.station} — ${cast.lat.toFixed(4)}\\u00b0N, ${Math.abs(cast.lon).toFixed(4)}\\u00b0W<br>` +
                   `<span style="font-size:10px">${cast.time}</span>`, font: { size: 12 } },
    margin: { l: 55, r: 20, t: 55, b: 45 },
    xaxis: { title: { text: 'Dissolved oxygen (mg/L)', font: { size: 11 } } },
    yaxis: { title: { text: `Line length (${unit})`, font: { size: 11 } }, autorange: 'reversed' },
    shapes: shapes, annotations: annotations
  }, { displayModeBar: false, responsive: true }).then(() => Plotly.Plots.resize('chart'));
}

const legend = L.control({ position: 'bottomleft' });
legend.onAdd = () => {
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = '<b>DO</b><br>' +
    DATA.bins.slice().reverse().map(b => `<i style="background:${b[1]}"></i>${b[2]}`).join('<br>') +
    '<br><b>Route</b> <span style="border-top:2px solid #0B2C3D;display:inline-block;width:20px;"></span>';
  return div;
};
legend.addTo(map);

const units = L.control({ position: 'topright' });
units.onAdd = () => {
  const div = L.DomUtil.create('div', 'unitCtrl');
  div.innerHTML = '<strong>Length units</strong><br>' +
    '<label><input type="radio" name="length-unit" value="m" checked> meters</label>' +
    '<label><input type="radio" name="length-unit" value="fathoms"> fathoms</label>';
  L.DomEvent.disableClickPropagation(div);
  return div;
};
units.addTo(map);

document.querySelectorAll('input[name="length-unit"]').forEach(el => {
  el.addEventListener('change', (evt) => {
    lengthUnit = evt.target.value;
    if (selectedCast) showCast(selectedCast);
  });
});

document.getElementById('aboutBody').innerHTML = DATA.provenance
  .map(([title, items]) => `<h3>${title}</h3><ul>${items.map(i => `<li>${i}</li>`).join('')}</ul>`).join('');

const info = L.control({ position: 'bottomright' });
info.onAdd = () => {
  const div = L.DomUtil.create('div', 'infoBtn');
  div.innerHTML = 'i';
  div.title = 'Sources, filtering and assumptions';
  L.DomEvent.disableClickPropagation(div);
  div.onclick = () => { document.getElementById('about').style.display = 'block'; };
  return div;
};
info.addTo(map);
</script>
</body>
</html>
"""


def main():
    args = parse_args()
    df = pd.read_csv(args.csv, parse_dates=["timestamp"])

    derived = build_derived(df)
    derived.to_csv(args.derived, index=False)
    print(f"Wrote {args.derived}: {len(derived)} readings across {derived['station'].nunique()} casts, "
          f"{int(derived['not_on_bottom'].sum())} tagged not-on-bottom")

    casts = cast_records(derived, args.timezone)

    route = pd.DataFrame({
        "lat": df["global_position_int.lat"] / 1e7,
        "lon": df["global_position_int.lon"] / 1e7,
    }).dropna()

    base = product_base_name(args.callsign, route, df["timestamp"], args.timezone, args.location)
    if args.pdf is None:
      args.pdf = ROOT / f"outputs/{base}_casts.pdf"
    if args.html is None:
      args.html = ROOT / f"outputs/{base}_casts_map.html"

    bounds = data_bounds(route["lat"], route["lon"], args.pad_deg)
    dem_path = args.dem or fetch_dem(
        bounds, args.arcsec,
        ROOT / f"data/bathymetry/{slug(args.callsign)}_dem_{args.arcsec:.3f}arcsec.tif",
        refresh=args.refresh_dem)
    dem, extent = load_dem(dem_path, bounds)

    write_pdf(args.pdf, casts, dem, extent, route, args.callsign)
    print(f"Wrote {args.pdf}")

    write_html(args.html, casts, route, args.callsign)
    print(f"Wrote {args.html}")

    index_path = ROOT / "index.html"
    write_outputs_index(index_path, ROOT / "outputs")
    print(f"Wrote {index_path}")


if __name__ == "__main__":
    main()
