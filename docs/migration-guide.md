# Migration guide

A complete, in-order runbook from a raw adidas Running / Runtastic export to
activities on Strava. For background on why this exists, see the
[README](../README.md); for the export layout, see
[adidas export format](adidas-export-format.md); for the optional API path
in detail, see [Strava API setup](strava-api-setup.md).

## 0. Install

Requires Python 3.12 or newer.

```bash
git clone <repository-url>
cd adidas-to-strava
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

`.[dev]` also installs `pytest` and `ruff`, used in [Development](#development)
below. If you only want to run the CLI, `python3 -m pip install -e .` is
enough.

All commands below assume the virtual environment is active.

## 1. Locate your export

You need the folder produced by adidas Running / Runtastic's data export
(commonly named like `export-YYYYMMDD-NNN/`). It must contain a
`Sport-sessions/` directory — see
[adidas export format](adidas-export-format.md) for the full layout.

## 2. Inspect (read-only)

`inspect` reads and filters the export without writing anything:

```bash
python3 -m adidas_to_strava inspect \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running
```

It reports how many sessions were scanned, how many were excluded by date or
sport, how many were malformed, and how many eligible runs have matching
GPS, heart-rate, and separate-elevation companion files. Those stream counts
show file presence, not successful parsing or usable measurements. Use this
to sanity-check a date range or export before writing any files.

Date filtering notes:

- `--since` and `--until` are both optional; omitting both means no date
  filtering at all.
- Both bounds are **inclusive**.
- Dates are the activity's **local calendar date**, computed from its actual
  `start_time` plus the exported timezone offset when that offset is usable.
  If it is missing or unusable, filtering falls back to the UTC calendar
  date. The filename and your machine's current timezone are not used.
- `--since` later than `--until` is rejected with an error.

## 3. Dry-run convert

Before writing anything, confirm what a real conversion would do:

```bash
python3 -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --output ./output \
  --dry-run
```

`--dry-run` applies normal selection and parses candidates that are not
short-circuited by the existing-output/converted-manifest duplicate check.
It writes no TCX files, manifest, or manifest rows.

## 4. Convert one session first

Pick a single known session and select only it, so you can inspect one real
output before trusting the rest:

```bash
python3 -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --session-id 9490f545-63f9-4b34-bc46-7589427f3eb8 \
  --output ./output
```

If conversion writes a `.tcx`, open it (or check its new/updated row in
`output/manifest.csv`) and confirm the date, distance, and duration look
right before continuing. An existing duplicate or unusable GPS stream can
result in no new TCX.

## 5. Full conversion

```bash
python3 -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --output ./output
```

An existing TCX for a session is left alone on rerun unless you pass
`--overwrite`. A session already recorded as `converted` in the manifest
stays `converted` on a repeat run — this is what makes reconversion
duplicate-safe.

## 6. Verify the manifest

For each session processed by a real conversion, `output/manifest.csv`
inserts or replaces a row containing associated source paths, the target TCX
path, `status` (`converted`, `duplicate`, `skipped_no_gps`, or `error`), and
parser/write details. Rows from earlier conversions of other sessions remain
in the file; a dry-run adds none. Skim it for `skipped_no_gps` or `error`
rows before moving on — see
[adidas export format](adidas-export-format.md#the-conversion-manifest) for
the full column reference and
[troubleshooting](troubleshooting.md) if something looks wrong.

## 7. Upload

Pick one of the two paths below (they're not mutually exclusive — you can
convert once and choose per activity, but most people pick one path for a
whole migration).

### 7a. Manual: Strava's web bulk uploader

No Strava API app, no OAuth, no tokens needed. Go to Strava's bulk upload
page and select the `.tcx` files from `./output`:

<https://strava.zendesk.com/hc/en-us/articles/216918007-Bulk-Uploading-Activities-to-Strava>

File-count and other limits are set by Strava, vary by plan, and can change —
check that page rather than relying on a fixed number here. If Strava reports
a file as a duplicate, treat that portal result as authoritative for that
upload rather than assuming duplicate handling is guaranteed in advance.

### 7b. API-assisted upload

One-time setup: follow [Strava API setup](strava-api-setup.md) fully before
this step (create app, `.env`, `auth`).

Always dry-run first, then attempt one actionable candidate, then verify:

```bash
python3 -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --limit 1 \
  --dry-run \
  --verbose

python3 -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --limit 1 \
  --verbose
```

If the candidate passes the pre-submit checks and completes, check that
activity on strava.com. Once you're satisfied, drop `--limit` for the rest:

```bash
python3 -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running
```

`upload` has no `--session-id` flag. Without `--force`, `--limit 1` selects
the first actionable candidate after completed/recognized-duplicate state is
skipped; it can still produce no upload if that candidate fails the
pre-submit checks. See [Strava API setup](strava-api-setup.md) for how
resuming, duplicate handling, `--force`, and rate limits work.

## 8. Verify end to end

- Check the manifest rows for the sessions you intended to convert. If you
  reused an output directory, remember that rows from earlier conversions
  are preserved.
- Spot-check a handful of activities in Strava for correct date, distance,
  and GPS track.
- Expect Strava to recompute derived metrics (elevation gain, moving time,
  pace, segments, best efforts) after import — that's normal, not a
  conversion error. See the fidelity table in the
  [README](../README.md#data-fidelity).

## Exact CLI reference

### `inspect`

Read-only; writes nothing.

| Argument | Type | Default | Description |
|---|---|---|---|
| `export_path` | positional path | — | Path to the adidas/Runtastic export root |
| `--since` | `YYYY-MM-DD` | none | Include activities on or after this local date (inclusive) |
| `--until` | `YYYY-MM-DD` | none | Include activities on or before this local date (inclusive) |
| `--sport` | choice | `running` | Only `running` is currently supported |
| `--limit` | positive int | none | Consider the first N eligible sessions ordered by local date, then session ID |
| `--session-id` | UUID string | none | Restrict to one specific adidas session |
| `--verbose` | flag | off | Enable debug logging |

### `convert`

Writes TCX files and `manifest.csv` under `--output` (unless `--dry-run`).

| Argument | Type | Default | Description |
|---|---|---|---|
| `export_path` | positional path | — | Path to the adidas/Runtastic export root |
| `--since` / `--until` / `--sport` | — | — | Same accepted values and filtering meaning as `inspect` |
| `--limit` | positive int | none | Consider the first N eligible sessions ordered by UTC start time, then session ID |
| `--session-id` | string | none | Restrict to one specific adidas session |
| `--output` | path | `./output` | Output directory for TCX files and `manifest.csv` |
| `--dry-run` | flag | off | Parse selected non-duplicate candidates but write no TCX or manifest |
| `--overwrite` | flag | off | Replace an existing TCX for the same session |
| `--verbose` | flag | off | Enable debug logging |

### `auth`

One-time (or occasional) Strava OAuth authorization. Only needed for the API
upload path. See [Strava API setup](strava-api-setup.md) for full detail.

| Argument | Type | Default | Description |
|---|---|---|---|
| `--env-file` | path | `./.env` | Location of the `.env` file to read and update |
| `--code` | string | none | Exchange an already-obtained one-use authorization code directly |
| `--port` | positive int | `8765` | Local port for the OAuth callback listener |
| `--timeout` | positive int (seconds) | `180` | How long to wait for the callback |
| `--no-browser` | flag | off | Don't attempt to open a browser automatically |
| `--verbose` | flag | off | Enable debug logging |

### `upload`

Submits converted TCX files to Strava. Only needed for the API upload path.

| Argument | Type | Default | Description |
|---|---|---|---|
| `output_dir` | positional path | — | Directory containing `manifest.csv` and TCX files (the `--output` from `convert`) |
| `--since` / `--until` / `--sport` | — | — | Same accepted values and filtering meaning as `inspect`, applied to manifest candidates |
| `--limit` | positive int | none | Select the first N actionable candidates ordered by local date/session ID; without `--force`, completed and recognized-duplicate rows are skipped before counting |
| `--dry-run` | flag | off | Parse the manifest and perform XML/required-structure checks; zero Strava requests and state writes |
| `--force` | flag | off | Resubmit sessions Strava/local state already consider complete; may create duplicates |
| `--env-file` | path | `./.env` | Location of the `.env` file with Strava credentials |
| `--verbose` | flag | off | Enable debug logging |

## Development

```bash
source .venv/bin/activate
ruff format .
ruff check .
pytest
```

Tests use fake HTTP sessions and never make real Strava API requests.
