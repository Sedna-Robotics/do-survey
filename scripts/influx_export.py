"""Export selected vehicle telemetry from InfluxDB to CSV.

global_position_int: lat/lon/alt/hdg, downsampled to 1 Hz
profile_sample:      all fields, native rate
winch-status / winch_status2: line_length/speed, downsampled to 1 Hz
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.rest import ApiException

ROOT = Path(__file__).resolve().parents[1]


def slug(callsign):
    return callsign.strip().lower().replace(" ", "-")

INFLUX_URL = os.environ.get("INFLUX_URL", "https://us-east-1-1.aws.cloud2.influxdata.com")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "Engineering")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "warden")

# measurement -> fields (None = all fields)
DOWNSAMPLED = {
    "global_position_int": ["lat", "lon", "alt", "hdg"],
}
OUTPUT_DOWNSAMPLED = {
    **DOWNSAMPLED,
    "winch_status": ["line_length", "speed", "state_enum"],
}
FULL_RATE = {"profile_sample": None}

UTC = ZoneInfo("UTC")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--callsign", required=True, help="Vehicle callsign, e.g. warden-2")
    p.add_argument("--start", required=True, help="Local start, e.g. 2026-08-19T19:00")
    p.add_argument("--stop", default=None, help="Local stop (default: now)")
    p.add_argument("--timezone", default="America/New_York",
                   help="IANA zone that --start and --stop are expressed in")
    p.add_argument("--out", default=None,
                   help="Output CSV (default data/raw/<callsign>_export.csv)")
    p.add_argument("--chunk-minutes", type=int, default=30, help="Query window size; smaller avoids cloud query timeouts")
    args = p.parse_args()
    if args.out is None:
        args.out = ROOT / f"data/raw/{slug(args.callsign)}_export.csv"
    return args


def to_utc(local_str, timezone):
    return datetime.strptime(local_str, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def windows(start, stop, minutes):
    cursor = start
    step = timedelta(minutes=minutes)
    while cursor < stop:
        yield cursor, min(cursor + step, stop)
        cursor += step


def resolve_callsign(client, wanted, start, stop):
    """Match the requested callsign case-insensitively against the bucket's tag values."""
    query = f'''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{INFLUX_BUCKET}", tag: "callsign", start: {fmt(start)}, stop: {fmt(stop)})
'''
    values = [r.get_value() for t in client.query_api().query(query) for r in t.records]
    for v in values:
        if v.lower() == wanted.lower():
            return v
    raise SystemExit(f"callsign {wanted!r} not found. Available: {values}")


def winch_spec(callsign):
    if callsign.lower() == "warden-2":
        return "winch-status", ["line_length", "speed"]
    return "winch_status2", ["line_length_m", "state_enum"]


def _selector(spec):
    clauses = []
    for measurement, fields in spec.items():
        if fields is None:
            clauses.append(f'r._measurement == "{measurement}"')
        else:
            field_or = " or ".join(f'r._field == "{f}"' for f in fields)
            clauses.append(f'(r._measurement == "{measurement}" and ({field_or}))')
    return " or ".join(clauses)


def run(client, query, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            rows = [
                {
                    "time": rec.get_time(),
                    "measurement": rec.get_measurement(),
                    "field": rec.get_field(),
                    "value": rec.get_value(),
                }
                for rec in client.query_api().query_stream(query)
            ]
            return pd.DataFrame(rows, columns=["time", "measurement", "field", "value"])
        except ApiException as exc:
            if exc.status < 500 or attempt == attempts:
                raise
            backoff = 2 ** attempt
            print(f"  query failed ({exc.status}), retry {attempt}/{attempts - 1} in {backoff}s")
            time.sleep(backoff)


def fetch(client, callsign, start, stop, chunk_minutes):
    source_winch_measurement, source_winch_fields = winch_spec(callsign)
    downsampled = {
        **DOWNSAMPLED,
        source_winch_measurement: source_winch_fields,
    }
    frames = []
    for win_start, win_stop in windows(start, stop, chunk_minutes):
        base = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {fmt(win_start)}, stop: {fmt(win_stop)})
  |> filter(fn: (r) => r.callsign == "{callsign}")
'''
        chunk = pd.concat([
            run(client, f'''{base}
    |> filter(fn: (r) => {_selector(downsampled)})
  |> aggregateWindow(every: 1s, fn: last, createEmpty: false)
    |> map(fn: (r) => ({{r with _measurement: if r._measurement == "{source_winch_measurement}" then "winch_status" else r._measurement, _field: if r._field == "line_length_m" then "line_length" else r._field}}))
'''),
            run(client, f'''{base}
  |> filter(fn: (r) => {_selector(FULL_RATE)})
'''),
        ], ignore_index=True)
        print(f"  {fmt(win_start)} -> {fmt(win_stop)}: {len(chunk):,} points")
        frames.append(chunk)
    return pd.concat(frames, ignore_index=True)


def main():
    args = parse_args()
    token = os.environ.get("INFLUX_TOKEN")
    if not token:
        sys.exit("INFLUX_TOKEN environment variable is required")

    start = to_utc(args.start, args.timezone)
    stop = to_utc(args.stop, args.timezone) if args.stop else datetime.now(UTC)
    print(f"Window: {fmt(start)} -> {fmt(stop)} (UTC), {args.chunk_minutes} min chunks")

    with InfluxDBClient(url=INFLUX_URL, token=token, org=INFLUX_ORG, timeout=900_000) as client:
        callsign = resolve_callsign(client, args.callsign, start, stop)
        print(f"Using callsign tag value: {callsign}")
        df = fetch(client, callsign, start, stop, args.chunk_minutes)

    if df.empty:
        sys.exit("No data returned for this window.")

    print(f"Retrieved {len(df):,} points")
    print(df.groupby(["measurement", "field"]).size().to_string())

    df["col"] = df["measurement"] + "." + df["field"]
    df["occurrence"] = df.groupby(["time", "col"]).cumcount()
    wide = df.pivot(index=["time", "occurrence"], columns="col", values="value").sort_index()
    assert wide.count().sum() == len(df), "point count mismatch"

    if wide.index.get_level_values("occurrence").max() == 0:
        wide = wide.droplevel("occurrence")
    wide.index.name = "timestamp"

    ordered = [
        f"{m}.{f}"
        for m, fields in (*OUTPUT_DOWNSAMPLED.items(), *FULL_RATE.items())
        for f in (fields if fields else sorted(df.loc[df.measurement == m, "field"].unique()))
        if f"{m}.{f}" in wide.columns
    ]
    wide = wide[ordered]

    wide.to_csv(args.out)
    print(f"Wrote {args.out} ({len(wide):,} rows, {len(wide.columns)} columns)")


if __name__ == "__main__":
    main()
