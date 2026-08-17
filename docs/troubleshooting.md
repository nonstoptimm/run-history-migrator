# Troubleshooting

Concise, recovery-focused answers. For normal usage, see the
[migration guide](migration-guide.md); for API-specific behavior, see
[Strava API setup](strava-api-setup.md).

### `No module named adidas_to_strava`

The virtual environment isn't active, or the package isn't installed. Either:

```bash
source .venv/bin/activate
```

or, from the project directory without activating:

```bash
PYTHONPATH=src python3 -m adidas_to_strava --help
```

### `missing Sport-sessions directory: ...`

`export_path` doesn't point at a folder containing `Sport-sessions/`. Point
`inspect`/`convert` at the export root itself (see
[adidas export format](adidas-export-format.md#naming-and-tree)), not a
parent folder or a subfolder inside it.

### `Eligible runs: 0` / nothing to convert

In order of likelihood:

1. **Date range too narrow or wrong.** Rerun `inspect` with no `--since`/
   `--until` at all to see the full range of dates present, then narrow from
   there. Remember dates are the session's local date, not UTC or filename
   date — see [dates and timezones](#a-run-lands-on-the-wrong-day) below.
2. **Wrong sport.** Only `running` (adidas sport type ID `1`) is supported;
   other sports are excluded, not miscounted as running.
3. **All sessions malformed.** Check the `Malformed sessions` count in the
   `inspect` report; a non-zero count with warnings logged (`--verbose`)
   usually means a session JSON is missing its `id` or `start_time`.
4. **Wrong export path.** Confirm `Sport-sessions/*.json` files actually
   exist at the top level of the path you gave.

### A session is missing GPS, heart rate, or elevation

- **No usable GPS track**: the session is skipped entirely
  (`skipped_no_gps` in the manifest/summary) — this tool never fabricates a
  track. `inspect` only reports whether a matching companion file exists;
  conversion additionally requires parseable, timestamped rows.
- **Missing heart rate or elevation only**: conversion still succeeds; the
  TCX omits whichever values remain unavailable after fallback. This is
  expected when the export itself never captured them (e.g., no paired HR
  strap).

See [parser and fallback precedence](adidas-export-format.md#parser-and-fallback-precedence)
for exactly how GPS/HR/elevation are matched and prioritized.

### A TCX fails pre-submit checks

Before submitting, `upload` checks that the file is parseable XML containing
a Running activity, an activity start, and at least one trackpoint. This is a
required-structure check, not full TCX-schema or trackpoint-semantic
validation. A failure often means the file was hand-edited, corrupted, or
removed after conversion; the upload state records the error. Re-run
`convert --overwrite` for that session rather than editing the TCX by hand.

### A run lands on the wrong day

Local date is computed from the session's actual `start_time` plus the
exported timezone offset when that offset is usable. If it is missing or
unusable, the CLI falls back to the UTC calendar date. It never uses your
current machine's timezone or the export filename. For a run near midnight,
check the exported offset (for example after travel or a device timezone
error) and widen `--since`/`--until` by a day on each side if needed.

### Strava's bulk upload portal is rejecting files or behaving oddly

Manual upload limits (file count, size, rate) are set by Strava and can
change — check the current guidance at
<https://strava.zendesk.com/hc/en-us/articles/216918007-Bulk-Uploading-Activities-to-Strava>
rather than assuming a fixed number. Strava may report a file as a duplicate
based on its current service behavior; do not treat that as a guaranteed
preflight capability of the portal.

### OAuth callback never arrives / `auth` hangs

- Confirm the Strava application's **Authorization Callback Domain** is set
  to `localhost`.
- Check that the callback port (`8765` by default) isn't already in use;
  pick another with `auth --port PORT`.
- On a headless or remote machine, use `auth --no-browser` and paste the
  callback URL or code when prompted, or exchange a code you already have
  directly with `auth --code ONE_USE_CODE`.
- Increase `--timeout` if you need more time before the local listener gives
  up (default `180` seconds).

### `Strava authorization is missing required scope(s): ...`

Strava didn't grant one of the required scopes (`read`, `activity:write`).
Re-run `auth` and make sure to approve both when Strava's consent screen
asks — no partial authorization is saved.

### `STRAVA_CLIENT_ID is required` / `STRAVA_CLIENT_SECRET is required`

No value was found in the process environment or the expected `.env`, or the
value is blank. Confirm `.env` exists next to where you're running the
command, or pass its path explicitly with `--env-file /path/to/.env`.
Remember that existing `STRAVA_*` process variables override file values. See
[Strava API setup](strava-api-setup.md#2-create-your-local-env) for the
exact keys expected.

### An activity is skipped as already uploaded / possible duplicate

This is by design: a session already `completed` in
`output/upload-state.sqlite3` is skipped on rerun. A duplicate response is
reconciled automatically only when Strava's error contains an existing
activity ID in the format the CLI recognizes; otherwise the failure remains
visible and resumable in local state. Inspect `upload-state.sqlite3` before
doing anything else. Only pass `--force` if you've confirmed a resubmission
is actually intended — **`--force` can create a genuine duplicate activity
on Strava.**

### An upload was interrupted partway through

Just rerun the same `upload` command. A session that was `submitted` or
`processing` when interrupted resumes polling instead of resubmitting; a
session that never got past `submitting` will be retried fully. Nothing
extra to clean up.

### `Rate limit stop: ...` (exit code 2)

The CLI stopped proactively to avoid exhausting Strava's short-window or
daily rate limit (or Strava returned HTTP `429`). This is not a failure of
your data — simply wait for the stated reset (next 15-minute boundary, or
UTC midnight for a daily stop) and rerun the same command; already-completed
activities are skipped automatically. See
[rate limits](strava-api-setup.md#rate-limits) for how the reserve and
headers work.

### Distance, elevation, pace, or segments look different on Strava

Expected. Strava recalculates several derived metrics (elevation gain,
moving time, pace, segments, best efforts) from the trackpoints after
import — this tool converts the source stream faithfully but does not try to
match Strava's own recalculated numbers. See the fidelity table in the
[README](../README.md#data-fidelity).

### Relative Effort is missing on an imported activity

Relative Effort is calculated and exposed by Strava according to the inputs
available on the activity and current account/service behavior. Missing
source heart rate commonly explains why no heart-rate-derived Relative
Effort appears, though Strava may support other user-supplied inputs. The
activity's age and manual-versus-API upload path do not add heart rate data
that was absent from the adidas export.

### Where are my credentials and state actually stored?

- `.env` — the normal persistent store, defaulting to the current directory
  and overridable with `--env-file` (or the default-path environment
  setting). Process `STRAVA_*` variables can supply or override its values;
  credentials also exist in process memory/environment while the CLI runs.
- `output/upload-state.sqlite3` — inside the `--output`/`output_dir` you
  used for `convert`/`upload`. Contains upload lifecycle state per session.

Both are excluded from version control by this project's `.gitignore`; treat
both as sensitive and don't share them.
