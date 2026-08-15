# adidas Running export to Strava TCX

This Python 3.12+ command-line tool converts local adidas Running (formerly
Runtastic) export activities into individual TCX files for manual Strava
upload. It does not connect to or upload anything to Strava.

## Expected export structure

```text
export-YYYYMMDD-NNN/
└── Sport-sessions/
    ├── YYYY-MM-DD_HH-MM-SS-UTC_SESSION-ID.json
    ├── GPS-data/
    │   ├── ...SESSION-ID.json
    │   └── ...SESSION-ID.gpx
    ├── Heart-rate-data/
    │   └── ...SESSION-ID.json
    └── Elevation-data/
        └── ...SESSION-ID.json
```

adidas and Runtastic naming is inconsistent across export generations. The
tool scans only top-level `Sport-sessions/*.json` files as session metadata and
matches companion data by the session ID, not by filename timestamps.

GPS JSON is preferred. Timestamped GPX is used as a fallback. Optional HR and
elevation measurements are attached to existing GPS points by nearby
timestamps; no trackpoints are invented. Activities without a usable
timestamped GPS stream are skipped with a warning.

## Usage

Run commands from this project directory:

```bash
PYTHONPATH=src python -m adidas_to_strava convert \
  /path/to/export-20260813-000 \
  --dry-run
```

Convert one known session:

```bash
PYTHONPATH=src python -m adidas_to_strava convert \
  /path/to/export-20260813-000 \
  --session-id 9490f545-63f9-4b34-bc46-7589427f3eb8 \
  --output ./output
```

Bulk-convert running activities from 2023 onward:

```bash
PYTHONPATH=src python -m adidas_to_strava convert \
  /path/to/export-20260813-000 \
  --since 2023-01-01 \
  --sport running \
  --output ./output
```

Defaults are `--since 2023-01-01`, `--sport running`, and `--output ./output`.
Other options are `--limit N`, `--session-id ID`, `--verbose`, `--dry-run`,
and `--overwrite`.

Existing session outputs are skipped unless `--overwrite` is supplied. Each
non-dry run also writes `manifest.csv` in the output directory with source,
output, status, and warning details. TCX timestamps are UTC; filenames use the
activity's exported timezone offset when available.

## Uploading to Strava

Open Strava's file upload page, select the generated `.tcx` files, and upload
them in batches within Strava's current file-count limits. Strava may
recalculate elevation, distance, moving time, pace, and related metrics from
the uploaded track.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
