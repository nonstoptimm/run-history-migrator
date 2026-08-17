# adidas-to-strava

Convert an adidas Running / Runtastic data export into standards-compliant TCX
files and, optionally, upload them to a private Strava account through the
official Strava API.

```text
adidas Running / Runtastic export
        |
        v
parse + join session data
        |
        v
local TCX files + conversion manifest
        |
        v
optional, resumable Strava API upload
```

## Why this exists

adidas Running historically used Runtastic infrastructure. Its data export
stores activity metadata separately from GPS, heart-rate, and elevation
streams. Strava does not directly import those adidas JSON files.

This project joins the streams by canonical adidas session ID, writes one TCX
file per activity, and can upload validated TCX files through Strava's
asynchronous activity upload API.

## Privacy and safety

- Conversion is local-first and does not require Strava credentials.
- Uploading is optional and uses the official API, not website scraping.
- `upload --dry-run` performs no API requests, token refreshes, or state writes.
- Successful uploads are recorded locally and skipped on later runs.
- Tokens are stored only in an ignored local `.env`.
- The tool never intentionally logs tokens, client secrets, or OAuth codes.

Never commit or share:

```text
.env
Client Secret
Access Token
Refresh Token
OAuth authorization codes
```

## Supported activities

Running is currently the only supported sport. It maps explicitly to
adidas/Runtastic sport type ID `1`. Other sports are rejected rather than
silently treated as running.

## Supported export structure

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

adidas and Runtastic filenames vary between export generations. The converter:

- scans only top-level `Sport-sessions/*.json` metadata files
- uses the session's `id` as the canonical companion-stream key
- prefers timestamped GPS JSON and falls back to timestamped GPX
- joins optional heart rate and elevation by nearby source timestamps
- never fabricates trackpoints or timestamps
- skips activities with no usable timestamped GPS track

## Requirements and installation

Python 3.12 or newer is required.

```bash
git clone <repository-url>
cd adidas-to-strava
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Development tools:

```bash
python -m pip install -e '.[dev]'
```

All examples below assume the virtual environment is active. You can also use
`python -m adidas_to_strava` directly with an appropriate `PYTHONPATH`.

## Inspect an export

Inspect reads and filters the export without creating files:

```bash
python -m adidas_to_strava inspect \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running
```

## Date filtering

Both date flags are optional:

- `--since YYYY-MM-DD`: include activities on or after the date
- `--until YYYY-MM-DD`: include activities on or before the date

Both boundaries are inclusive. Dates use the activity's local calendar date,
calculated from its actual session `start_time` and exported timezone offset.
Filenames are not used as the authoritative date. Supplying no date flags
means no date filtering. A range where `--since` is later than `--until` is
rejected.

## Convert to TCX

Start with a dry run:

```bash
python -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --output ./output \
  --dry-run
```

Then create the TCX files:

```bash
python -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --output ./output
```

Useful options:

```text
--session-id UUID   select one known adidas session
--limit N           process only the first N eligible sessions
--overwrite         replace an existing TCX for the same session
--verbose           enable diagnostic logging
```

Single-session example:

```bash
python -m adidas_to_strava convert \
  ~/Downloads/export-20260813-000 \
  --session-id 9490f545-63f9-4b34-bc46-7589427f3eb8 \
  --output ./output
```

## Conversion manifest

`output/manifest.csv` connects every eligible adidas session to its source
metadata, companion streams, output TCX, status, warning, timestamps, and local
date. The canonical `session_id` remains available for duplicate-safe upload
tracking.

Existing TCX files are skipped unless `--overwrite` is supplied. A prior
successful manifest record remains successful on a duplicate-safe rerun.

## Create a private Strava API application

1. Open Strava's API settings page: <https://www.strava.com/settings/api>
2. Create an application for your personal migration.
3. Set the callback domain to `localhost`.
4. Copy `.env.example` to `.env`.
5. Add the application's client ID and client secret:

```bash
cp .env.example .env
chmod 600 .env
```

```dotenv
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
STRAVA_ACCESS_TOKEN=
STRAVA_REFRESH_TOKEN=
STRAVA_TOKEN_EXPIRES_AT=
```

The requested scopes are the minimum used by this tool:

```text
read
activity:write
```

## Authorize with Strava

```bash
python -m adidas_to_strava auth
```

The command prints the official authorization URL, opens it in your browser,
and listens temporarily on `http://localhost:8765/callback`. If the callback
cannot be captured, it asks for the returned authorization code or full
callback URL.

For manual exchange:

```bash
python -m adidas_to_strava auth --code ONE_USE_CODE
```

Access tokens expire after roughly six hours. Before a real API request the CLI
refreshes an expired token using the latest refresh token. Strava refresh
tokens rotate, so the CLI atomically updates `.env` with the newest access
token, refresh token, and expiry.

## Upload dry run

Always inspect the upload selection first:

```bash
python -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --limit 1 \
  --dry-run \
  --verbose
```

Dry-run validates the conversion manifest and TCX XML. It makes zero Strava
requests and does not create upload state.

## First safe upload: one activity

After reviewing the dry run, upload exactly one activity:

```bash
python -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running \
  --limit 1 \
  --verbose
```

Verify the resulting activity in Strava before proceeding.

## Bulk upload

Only after the one-activity verification:

```bash
python -m adidas_to_strava upload ./output \
  --since 2023-01-01 \
  --until 2026-08-13 \
  --sport running
```

Strava uploads are asynchronous. For each file the CLI submits the upload,
persists its upload ID, polls at a conservative interval, records the resulting
activity ID, and reports terminal processing errors.

The uploader leaves the optional activity name unset so Strava applies its
normal time-of-day activity naming. You can rename an activity in Strava later.

## Resume and duplicate handling

`output/upload-state.sqlite3` is a local SQLite database keyed by adidas
session UUID. It records:

- source TCX
- attempt/submission/completion timestamps
- Strava upload ID
- Strava activity ID
- lifecycle status
- error or warning

Completed sessions are skipped. Interrupted sessions that already have a
Strava upload ID resume polling instead of submitting again.

`--force` intentionally bypasses completed-state protection and may create a
duplicate Strava activity. Use it only after investigating the existing state.

## Rate limits and retries

The default Strava application limits are currently:

```text
Overall: 200 requests / 15 minutes, 2,000 requests / day
Read:    100 requests / 15 minutes, 1,000 requests / day
```

Runtime response headers are authoritative. The CLI tracks Strava's overall
and read-limit headers, keeps a conservative reserve, and stops safely before
exhaustion. HTTP 429 stops immediately. Daily exhaustion requires waiting until
midnight UTC; short-term windows reset at natural 15-minute boundaries.

Timeouts, connection failures, and selected transient server responses use
bounded exponential backoff. Permanent client errors are not retried.

## TCX behavior

Generated TCX includes available:

- Running activity type
- UTC timestamps
- GPS position
- source distance
- altitude/elevation
- heart rate
- calories
- activity duration and total distance

Pauses remain visible through source timestamp gaps. Missing heart rate or
elevation does not prevent conversion. Strava may recalculate distance,
elevation, moving time, pace, or related metrics after upload.

## Troubleshooting

**`No module named adidas_to_strava`**

Activate the virtual environment after installing the project, or run from the
project directory with:

```bash
PYTHONPATH=src python -m adidas_to_strava --help
```

**OAuth callback does not arrive**

Confirm the Strava callback domain is `localhost`, check that port 8765 is
available, or use the printed URL and paste the callback URL/code when
prompted. A different port can be selected with `auth --port PORT`.

**Activity is skipped as already uploaded**

Inspect `output/upload-state.sqlite3`. Do not delete or use `--force` until you
have verified whether the corresponding Strava activity exists.

**Manifest path cannot be resolved**

Keep `manifest.csv` with its generated TCX directory. Historical manifests
that contain export-relative paths are supported when the project remains
inside or beside the original export.

## Security and revocation

- Keep `.env` mode `600` on shared systems.
- Never paste credentials into issues, commits, shell transcripts, or logs.
- Rotate the client secret in Strava if it may have been exposed.
- Revoke application access from Strava account settings when migration is
  complete.
- If the private app is no longer needed, delete it from
  <https://www.strava.com/settings/api>.
- Delete local `.env` and `upload-state.sqlite3` when you no longer need them.

## Limitations

- Running only
- One-athlete personal CLI workflow
- TCX uploads only
- No automatic recovery of an activity ID if Strava accepted an upload but
  never returned a usable upload record
- No YAML mapping or preset configuration

## Future ideas

- Additional explicit sport mappings
- YAML presets for sport mapping, metadata, naming, and import policies
- Richer inspection reports
- Export/backup of upload state
- Optional activity naming and description templates

## Development

```bash
source .venv/bin/activate
ruff format .
ruff check .
pytest
```

Tests use fake HTTP sessions and never make real Strava API requests.
