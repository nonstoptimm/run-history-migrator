# adidas-to-strava

Convert a historical adidas Running / Runtastic data export into TCX files
you can put on Strava — either by hand through
Strava's own upload page, or, optionally, through Strava's official API using
this tool's built-in, resumable uploader.

## So what

If you've ever tried to drag an adidas Running export straight into Strava,
it doesn't work — the data is split across several JSON files per run, and
Strava has no idea how to read them. This tool joins those files back into
one TCX per run, locally, with nothing sent anywhere unless you explicitly
choose the API upload path. You end up with a folder of TCX v2 files
plus a manifest you can check before uploading anything, and — if you want
it — a duplicate-safe, resumable way to push them to Strava via its API.

## Who this is for

- Runners moving years of adidas Running / Runtastic history to Strava once,
  as a one-time migration.
- People who want to inspect and verify their data locally before anything
  touches Strava.
- Anyone comfortable running a couple of commands in a terminal.

## Who this isn't for

- Non-running activities — only running is currently supported (see
  [Limitations](#limitations)).
- Ongoing/continuous sync between adidas and Strava — this is a one-time
  conversion tool, not a background sync service.
- Anyone needing a no-terminal, point-and-click GUI.

## Why conversion is needed

adidas Running historically ran on Runtastic's infrastructure. Its data
export stores each run's metadata separately from its GPS, heart-rate, and
elevation streams, in adidas/Runtastic's own JSON shape — not a format Strava
imports directly. This tool reads those files, joins them back together by
each run's canonical session ID, and writes one TCX file per eligible run for
inspection and upload.

## How it works

```text
1. Locate your adidas/Runtastic export folder
2. inspect it (read-only) to see what's eligible
3. convert eligible runs into local TCX files + a manifest
4. spot-check one converted file before trusting the rest
5. upload — by hand on strava.com, or via this tool's optional API uploader
```

## Two ways to get activities onto Strava

Both start from the same local `convert` step. Pick one for the upload
itself.

### A — Manual, through Strava's web portal

Convert locally, then upload the resulting `.tcx` files yourself through
Strava's own bulk uploader. No Strava API application, no OAuth, no tokens.

### B — Optional, API-assisted upload

Create a small personal Strava API application once, authorize this CLI, and
let it submit TCX files through Strava's official asynchronous upload API —
tracking state locally so reruns skip known completions, resume known
in-flight uploads, and reconcile duplicate responses that include a
recognizable existing activity ID.

| Situation | Recommended path |
|---|---|
| A handful of runs, doing this once | **A** — manual portal upload |
| Don't want to create a Strava API app | **A** — manual portal upload |
| Hundreds of historical runs | **B** — API-assisted, resumable |
| Want local completion tracking and resumable retries | **B** — API-assisted |
| No browser available for an OAuth callback | **A**, or **B** with `--no-browser` and manual code entry |

Full setup for path B lives in [Strava API setup](docs/strava-api-setup.md).

## Safety model: test one run first

Every step below is safe to run repeatedly and cheap to undo:

1. `inspect` — read-only; reports session metadata and matching companion-file
   presence without parsing every stream.
2. `convert --dry-run` — writes nothing; parses selected sessions unless an
   existing output/manifest row causes the normal duplicate bypass.
3. `convert --session-id <uuid>` (or `--limit 1`) — selects at most one run;
   it may write no TCX if that run is already present or has no usable GPS.
4. Open that one TCX (or check `manifest.csv`) before trusting the rest.
5. Only then run a full conversion.

For the API upload path specifically, repeat the same idea before touching
Strava: `upload --dry-run` (zero API calls) → `upload --limit 1` (attempt one
actionable candidate) → verify any resulting activity on strava.com → then
drop `--limit`.

## Data fidelity

| Data | Carries over? | Notes |
|---|---|---|
| GPS track | If present | JSON preferred, GPX fallback; skipped if neither has a usable timestamped track |
| Distance, duration | Yes | From the source session; pauses show as timestamp gaps, never interpolated |
| Heart rate | If present in export | Missing HR still converts fine; Strava features derived from HR may be unavailable |
| Elevation | If present in export | Falls back to inline GPS/GPX altitude when there's no separate elevation stream |
| Calories | If present at session top level | Uses the top-level session value; otherwise writes `0` |
| Activity type | Running only | Always written as Running |
| Segments, best efforts | No | Strava computes these itself after import |
| adidas challenges, groups, equipment | No | Not part of the data streams this tool reads |

This is not a claim of exact equivalence with the original adidas activity —
see [Limitations](#limitations) and [adidas export format](docs/adidas-export-format.md#fidelity-honestly).

## Recommended workflow

1. Install (see below).
2. `inspect` your export — read-only, inventories eligible metadata and
   companion-file presence.
3. `convert --dry-run` — previews selection and parses non-duplicate
   candidates without writing.
4. `convert --session-id <uuid>` — attempt one TCX and check it if written.
5. `convert` (full range) — write everything.
6. Check `manifest.csv` for `skipped_no_gps`/`error` rows.
7. Upload: manually on strava.com, **or** `upload --dry-run` →
   `upload --limit 1` → verify → `upload` (no `--limit`).

The full step-by-step, with every command shown, is in the
[migration guide](docs/migration-guide.md).

## Commands

| Command | Purpose | Key flags |
|---|---|---|
| `inspect` | Read-only look at what's eligible | `export_path`, `--since`, `--until`, `--sport running`, `--limit`, `--session-id`, `--verbose` |
| `convert` | Write TCX files + `manifest.csv` | selection flags, plus `--output`, `--dry-run`, `--overwrite`; see the guide for ordering |
| `auth` | One-time Strava OAuth (path B only) | `--env-file`, `--code`, `--port`, `--timeout`, `--no-browser`, `--verbose` |
| `upload` | Submit converted TCX to Strava (path B only) | `output_dir`, `--since`, `--until`, `--sport running`, `--limit`, `--dry-run`, `--force`, `--env-file`, `--verbose` |

`upload` has **no** `--session-id` — use `--limit 1` for single-activity
control. Full flag reference: [migration guide](docs/migration-guide.md#exact-cli-reference).

## Requirements and installation

Python 3.12 or newer.

```bash
git clone <repository-url>
cd adidas-to-strava
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

Drop `[dev]` (`python3 -m pip install -e .`) if you don't need the test/lint
tooling. All examples below assume the virtual environment is active.

## A concrete case

Jordan has three years of adidas Running history — about 600 runs — and
wants them on Strava without re-running anything or trusting a bulk import
blindly.

```bash
# 1. See what's there, no files written
python3 -m adidas_to_strava inspect ~/Downloads/export-20260813-000 --sport running

# 2. Narrow to the years that matter, still read-only
python3 -m adidas_to_strava inspect ~/Downloads/export-20260813-000 \
  --since 2023-01-01 --until 2026-08-13 --sport running

# 3. Dry-run the conversion for that range
python3 -m adidas_to_strava convert ~/Downloads/export-20260813-000 \
  --since 2023-01-01 --until 2026-08-13 --sport running --output ./output --dry-run

# 4. Convert just one known run and check the TCX by hand
python3 -m adidas_to_strava convert ~/Downloads/export-20260813-000 \
  --session-id 9490f545-63f9-4b34-bc46-7589427f3eb8 --output ./output

# 5. Convert everything in range
python3 -m adidas_to_strava convert ~/Downloads/export-20260813-000 \
  --since 2023-01-01 --until 2026-08-13 --sport running --output ./output
```

Jordan skims `output/manifest.csv`, sees a handful of early runs marked
`skipped_no_gps` (adidas never recorded a GPS track for those), and accepts
that. For the upload, Jordan sets up a personal Strava API app once (see
[Strava API setup](docs/strava-api-setup.md)), then:

```bash
python3 -m adidas_to_strava auth

python3 -m adidas_to_strava upload ./output \
  --since 2023-01-01 --until 2026-08-13 --sport running --limit 1 --dry-run --verbose

python3 -m adidas_to_strava upload ./output \
  --since 2023-01-01 --until 2026-08-13 --sport running --limit 1 --verbose
```

Only after checking that one activity on strava.com does Jordan drop
`--limit` and let the rest upload, trusting that an interrupted run can be
resumed by just running the same command again.

## Privacy and security

- Conversion is local-first; it never requires Strava credentials.
- The optional upload path uses Strava's official API only — never website
  scraping or an unofficial endpoint.
- `upload --dry-run` makes zero API requests, refreshes no token, and writes
  no upload state.
- A local, git-ignored `.env` is the normal credential store. Process
  environment variables can supply or override its values, and credentials
  necessarily exist in process memory/environment while the CLI runs. The
  tool never intentionally logs tokens, client secrets, or OAuth codes.
- Full detail, rotation, and cleanup: [Strava API setup](docs/strava-api-setup.md#secrets).

Never commit or share `.env`, a client secret, an access/refresh token, or an
OAuth authorization code.

## Limitations

- Running only — other sports are rejected, not silently treated as running.
- A one-athlete, personal CLI workflow, not a hosted service.
- One-time conversion/upload, not continuous sync.
- TCX output only.
- No automatic recovery of an activity ID if Strava accepted an upload but
  never returned a usable record for it.
- No YAML mapping or preset configuration (see docs for a future idea).

## What this is (and isn't)

This is an independent, unofficial personal-migration tool — it is not built
or endorsed by adidas, Runtastic, or Strava, and it doesn't replace or modify
either service's own export/import features. It's meant for moving your own
historical data once, not for ongoing sync, bulk automation on someone
else's behalf, or replicating adidas' own metrics exactly.

## FAQ

**Will my Strava stats match my adidas stats exactly?**
No — Strava recalculates several derived metrics (elevation gain, moving
time, pace, segments, best efforts) after import. See
[Data fidelity](#data-fidelity).

**Does this support cycling, swimming, or other sports?**
Not currently — running only, mapped explicitly to adidas sport type ID `1`.

**Do I have to use the Strava API at all?**
No — manual upload through Strava's web portal (path A) needs no API
application, OAuth, or tokens.

**Can rerunning `convert`/`upload` create duplicates?**
Normally, existing conversion outputs and locally completed uploads are
skipped. Recognizable Strava duplicate responses can also be reconciled;
`upload --force` deliberately resubmits and may create a duplicate. See
[Strava API setup](docs/strava-api-setup.md#duplicate-reconciliation).

**Something looks wrong — where do I look first?**
[Troubleshooting](docs/troubleshooting.md).

## Documentation

- [Migration guide](docs/migration-guide.md) — full install-to-upload
  runbook and exact CLI reference.
- [Strava API setup](docs/strava-api-setup.md) — optional app setup, OAuth,
  tokens, rate limits, duplicates, and cleanup.
- [adidas export format](docs/adidas-export-format.md) — export layout,
  parsing/fallback rules, TCX contents.
- [Troubleshooting](docs/troubleshooting.md) — concise, recovery-focused
  answers to common problems.
