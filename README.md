# Dissolved oxygen survey pipeline

Exports vehicle telemetry from InfluxDB, attaches bathymetric and tide-corrected
depth, and produces per-cast dissolved oxygen profiles as a PDF and an
interactive nautical chart.

## Layout

```
scripts/
  influx_export.py       InfluxDB -> data/raw/<callsign>_export.csv
  append_bathymetry.py   + depth columns -> data/processed/<callsign>_export_with_depth.csv
  cast_products.py       derived cast table, PDF and interactive chart
  ncei_dem.py            fetches DEM tiles from NOAA NCEI for a given extent
data/
  raw/                   unmodified export from InfluxDB
  processed/             depth-augmented export and the per-reading cast table
  bathymetry/            DEM GeoTIFF cache (not committed)
outputs/                 product PDFs/HTML and assets/
index.html               GitHub Pages landing page linking output products
```

## Running the pipeline

```bash
pip install -r requirements.txt

export INFLUX_TOKEN=...          # InfluxDB Cloud read token
python scripts/influx_export.py     --callsign warden-2 --start 2026-08-19T19:00
python scripts/append_bathymetry.py --callsign warden-2
python scripts/cast_products.py     --callsign warden-2
```

Optional naming override for friendlier product names:

```bash
python scripts/cast_products.py --callsign warden-2 --location "cape-cod-bay"
```

`--callsign` is required by all three scripts. It selects the InfluxDB tag value
and derives every default input and output path, so several vehicles can share
this tree without collision. The callsign is matched case-insensitively against
the tag values present in the bucket, and also appears in the plot titles and
the methods notes.

`--start` is required by `influx_export.py` and `--stop` defaults to now. Both
are interpreted in `--timezone` (default `America/New_York`), which is also the
zone `cast_products.py` uses for the cast times it prints on the plots.

## Bathymetry

DEM tiles are fetched on demand from the NOAA NCEI best-available mosaic, cut to
the bounding box of the positions actually present in the export plus
`--pad-deg`. Nothing about the survey area is hardcoded. `append_bathymetry.py`
fetches at `--arcsec 0.333` (~10 m) for depth sampling; `cast_products.py`
fetches at `--arcsec 3` for the map background. Rasters are cached under
`data/bathymetry/` and reused when a cached file covers the requested extent at
the requested resolution; pass `--refresh-dem` to force a re-download or `--dem`
to supply your own GeoTIFF.

By default, `append_bathymetry.py` uses the nearest NOAA CO-OPS water-level
station to the route centroid, matching the onboard AWS filter. Pass
`--station <id>` to select a specific station instead. The selected station and
VDatum offset are recorded in the processed CSV and reported in the products.

Every other input and output is overridable; see `--help`. Defaults resolve
relative to this directory, so the scripts work from any working directory.

`influx_export.py` reads `INFLUX_TOKEN` from the environment and also honours
`INFLUX_URL`, `INFLUX_ORG` and `INFLUX_BUCKET`. No credentials are stored in
this repository.

## Winch Telemetry And Cast Filtering

The exporter normalizes both winch telemetry formats to `winch_status` columns
in the raw CSV. `warden-2` reads `winch-status.line_length` and
`winch-status.speed`; all other callsigns read `winch_status2.line_length_m`
and `winch_status2.state_enum`. Thus downstream processing always uses
`winch_status.line_length`, plus `winch_status.speed` for warden-2 or
`winch_status.state_enum` for other vehicles.

For `winch_status2`, cast readings are on-bottom only when the latest reported
`state_enum` is `3`. That state persists until a newer state is reported. The
on-bottom transition must also be within `ON_BOTTOM_BATHY_TOLERANCE_M` of the
tide-adjusted bathymetry. This gate uses the same NCEI point lookup, VDatum
conversion, and nearest-station CO-OPS tide calculation as the onboard AWS
upload filter. For `warden-2`, the legacy rule applies: a reading is tagged
not-on-bottom when its winch speed exceeds `SPEED_THRESHOLD` or its line length
is below `MIN_LINE_LENGTH`.

## Products

`cast_products.py` writes products to `outputs/` using this default pattern:

`<callsign>_<location>_<start>-<end>_casts.pdf`

`<callsign>_<location>_<start>-<end>_casts_chart.html`

Where:
- `<callsign>` is slugged from `--callsign`
- `<location>` comes from `--location` when provided, otherwise auto-generated
  from route centroid coordinates
- `<start>-<end>` is the survey time range in `--timezone`, formatted as
  `YYYYMMDDTHHMM-YYYYMMDDTHHMM`

The PDF has three pages: a 2x5 grid of cast profiles (line length against
dissolved oxygen, points numbered and coloured by the DO scale, tagged points
greyed), the survey chart, and a methods page listing sources, filtering and
assumptions.

The chart HTML is self-contained apart from the Leaflet and Plotly CDNs. Click a
station for its cast inset; the `i` control repeats the methods notes. Serve it
over HTTP rather than opening via `file://` if your browser blocks local
scripts.

`index.html` (repo root) is regenerated each run as a GitHub Pages-friendly
landing page with Sedna branding and links to all `.html` and `.pdf` products
in `outputs/`.

## Key parameters

Defined at the top of `scripts/cast_products.py`:

| Constant | Value | Meaning |
| --- | --- | --- |
| `SPEED_THRESHOLD` | 0.3 m/s | warden-2: above this winch speed a reading is tagged not-on-bottom |
| `MIN_LINE_LENGTH` | 1.5 m | warden-2: below this line length a reading is tagged not-on-bottom |
| `ON_BOTTOM_BATHY_TOLERANCE_M` | 3.0 m | winch_status2: maximum shortfall from bathymetry at on-bottom transition |
| `VDATUM_OFFSET_M` | -1.764 m | MLLW to NAVD88 offset quoted in the methods notes |
| `TIDE_STATION` | 8447241 | fallback for legacy processed CSVs only |

`VDATUM_OFFSET_M` is a transcription of the value `append_bathymetry.py`
computes at runtime. New products read the recorded offset and tide station
from the processed CSV; the constants are retained only for legacy inputs.
