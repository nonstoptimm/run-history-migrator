# adidas / Runtastic export format

How this tool reads an adidas Running / Runtastic data export. Useful if a
session is unexpectedly skipped, or you want to understand exactly what data
feeds each TCX file. For the day-to-day commands, see the
[migration guide](migration-guide.md).

## Naming and tree

The export's top-level folder name (typically `export-YYYYMMDD-NNN/`) is not
significant to the tool — only the `Sport-sessions/` directory inside it
matters. A typical export looks like:

```text
export-YYYYMMDD-NNN/
└── Sport-sessions/
    ├── 2024-04-08_14-59-20-UTC_0019a46e-f34b-4f10-8cfa-49812a377532.json
    ├── GPS-data/
    │   ├── 2024-04-08_14-59-20-UTC_0019a46e-f34b-4f10-8cfa-49812a377532.json
    │   └── 2024-04-08_14-59-20-UTC_0019a46e-f34b-4f10-8cfa-49812a377532.gpx
    ├── Heart-rate-data/
    │   └── 2024-04-08_14-59-20-UTC_0019a46e-f34b-4f10-8cfa-49812a377532.json
    └── Elevation-data/
        └── 2024-04-08_14-59-20-UTC_0019a46e-f34b-4f10-8cfa-49812a377532.json
```

`inspect` and `convert` fail fast with a clear error if `Sport-sessions/` is
missing from the path you gave them.

## Top-level session discovery

Only the top-level `*.json` files directly inside `Sport-sessions/` are
treated as session records — the scan does not recurse, and it never treats a
file inside `GPS-data/`, `Heart-rate-data/`, or `Elevation-data/` as a
session. Each of those top-level files describes exactly one activity.

## Joining companion streams by UUID

Every session JSON carries a canonical `id` field — a UUID that is authoritative
regardless of filename. Companion files are matched to a session by taking the
segment **after the last underscore** in the companion filename's stem (for
example `..._0019a46e-f34b-4f10-8cfa-49812a377532.json`), which is expected to
be that same UUID. adidas and Runtastic have used different filename prefixes
across export generations; because the join key is only the trailing UUID
segment, the prefix format doesn't matter.

Filenames are never used as the source of truth for timing — only for locating
companion files.

## Parser and fallback precedence

**Session metadata** — top-level JSON fields are read first. `start_time`,
`duration`, `pause`, and `end_time` fall back to matching attributes in the
session's `initial_values` feature block; sport type falls back to
`initial_values.sport_type.id`; distance falls back from `track_metrics` to
`initial_values`. Calories use only the top-level session field and default
to `0` when it is absent or unusable. A session missing its canonical `id`,
a usable `start_time`, or a sport type is treated as malformed and skipped
with a warning rather than guessed at.

The local calendar date uses `start_time_timezone_offset` when it is usable.
If that offset is missing or unusable, the parser uses a zero offset, so date
filtering falls back to the UTC calendar date.

**GPS track** — GPS JSON is preferred. If it is missing, unreadable, or has no
usable timestamped rows, the parser falls back to the timestamped GPX file in
the same `GPS-data/` folder. Points from JSON and GPX are never mixed for the
same activity — one source wins entirely.

**Heart rate and elevation** — read from their own timestamped JSON stream
when present, and matched to each GPS point by nearest timestamp within a
5-second tolerance. A GPX file's inline `<hr>`/`<ele>` values are only used
for a point when no separate stream value is found within that tolerance.

**Nothing is fabricated.** Rows or values the parser cannot interpret are
dropped rather than guessed, and a session with no usable timestamped GPS
track at all is skipped rather than written with invented points. GPS JSON
receives explicit timestamp, finiteness, and coordinate-range checks. GPX is
parsed defensively as XML, but this is not full GPX-schema validation or an
exhaustive semantic range check of every GPX value.

## What ends up in the TCX

Each converted activity is one TCX `Activity` with `Sport="Running"`,
containing:

- `Id` — the session's UTC start timestamp
- One `Lap` with `TotalTimeSeconds`, `DistanceMeters`, `Calories` (the
  top-level session value, or `0` when absent),
  `Intensity="Active"`, `TriggerMethod="Manual"`
- A `Track` of `Trackpoint` elements, each with `Time` and `Position`
  (latitude/longitude), plus `AltitudeMeters`, `DistanceMeters`, and
  `HeartRateBpm` wherever that data is available for the point
- A `Notes` field recording the original adidas session ID, so the source
  session stays traceable from the TCX file alone

### Pauses

adidas records a total `pause` duration per session, and the underlying
gap between trackpoint timestamps reflects real pause windows. The converter
does not interpolate or fabricate points to fill a pause — the gap simply
remains visible in the trackpoint timeline, the same way it exists in the
source data.

### Missing streams

- No usable timestamped GPS at all → the session is skipped entirely
  (`skipped_no_gps` in the summary/manifest). A TCX with no trackpoints isn't
  produced.
- Missing heart rate or elevation alone does **not** block conversion. The
  TCX omits data that is unavailable; inline GPS/GPX altitude can still
  supply elevation when no separate stream contributes a value. The CLI
  summary reports aggregate `Missing HR` / `Missing elevation` counters, but
  the manifest has no dedicated columns for those counters.

## The conversion manifest

`output/manifest.csv` is keyed by canonical `session_id` (not by filename or
output path). A real conversion inserts or replaces rows for the selected
sessions while preserving rows already present for other sessions. A
`--dry-run` writes no manifest and adds no manifest rows.

| Column | Meaning |
|---|---|
| `session_id` | Canonical adidas UUID |
| `start_time` | UTC start timestamp |
| `sport_type_id` | adidas sport type ID (`1` for running) |
| `distance_m`, `duration_ms` | From the source session |
| `source_session_file` | Path to the original session JSON |
| `gps_file`, `heart_rate_file`, `elevation_file` | Companion paths associated with the conversion attempt; consult `status`/`warning` for usability |
| `output_tcx` | Target TCX path; it may not exist for skipped/error rows |
| `status` | `converted`, `duplicate`, `skipped_no_gps`, or `error` |
| `warning` | Parser/write details, or the reason a duplicate was bypassed |
| `local_start_date` | Local calendar date used for `--since`/`--until` filtering |
| `start_timezone_offset_ms` | Usable exported offset, or `0` for the UTC fallback |

The last two columns were added after the first release of this tool; older
manifests without them still work — the local date is recomputed from the
source session file (or from the UTC start time as a last resort) when
needed.

## Fidelity, honestly

This format mapping is deliberately conservative: it carries over what the
adidas export actually recorded, and skips or omits what it doesn't have. It
does not reconstruct segments, best efforts, or derived training metrics —
Strava computes those itself after import. See the fidelity table in the
[README](../README.md#data-fidelity) for a one-glance summary.
