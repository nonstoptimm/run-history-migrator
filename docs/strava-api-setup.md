# Strava API setup (optional)

This is only needed for the **API-assisted upload** path. If you're uploading
converted TCX files through Strava's own web bulk uploader instead, skip this
file entirely — see the [migration guide](migration-guide.md).

## Before you start

Creating and using a Strava API application is governed by Strava, not by
this tool, and its requirements have changed over time (for example around
subscription tiers or approval). Check the current requirements before
assuming free, instant access:

- Getting started: <https://developers.strava.com/docs/getting-started/>
- Your API application dashboard: <https://www.strava.com/settings/api>

## 1. Create a private API application

1. Open <https://www.strava.com/settings/api> and create an application for
   your own personal migration (not a public integration).
2. Set the **Authorization Callback Domain** to `localhost`.
3. Note the application's **Client ID** and **Client Secret**.

## 2. Create your local `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Credential/token keys used by the CLI:

| Key | You fill this in | Notes |
|---|---|---|
| `STRAVA_CLIENT_ID` | Yes | From your Strava API application |
| `STRAVA_CLIENT_SECRET` | Yes | From your Strava API application; treat like a password |
| `STRAVA_ACCESS_TOKEN` | No | Written automatically by `auth`, then refreshed automatically |
| `STRAVA_REFRESH_TOKEN` | No | Written by `auth`; later token responses may return an updated value |
| `STRAVA_TOKEN_EXPIRES_AT` | No | Unix timestamp, written automatically |

Only `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` need manual input. Leave
the token fields blank — `auth` fills them in.

By default the CLI looks for `.env` in the current directory; the
`ADIDAS_TO_STRAVA_ENV_FILE` process variable can change that default. Use
`--env-file /path/to/.env` on `auth` or `upload` to point at a different file
(for example when running from outside the project directory). Existing
`STRAVA_*` process environment variables take precedence over values loaded
from that file.

## 3. Authorize once

```bash
python3 -m adidas_to_strava auth
```

This prints the official Strava authorization URL, opens it in your browser,
and listens briefly on `http://localhost:8765/callback` for the redirect. The
CLI requests exactly the scopes it needs — `read` and `activity:write` — and
refuses to save an authorization that's missing either one.

Useful flags:

- `--port PORT` — use a different local callback port if `8765` is taken.
- `--timeout SECONDS` — how long to wait for the callback (default `180`).
- `--no-browser` — don't try to open a browser (headless/remote machines);
  the URL is still printed, and the CLI falls back to prompting for the
  pasted callback URL or code.
- `--code ONE_USE_CODE` — skip the local callback server entirely and
  exchange an authorization code you already have (useful for fully
  non-interactive or remote setups).

If the callback can't reach the CLI at all, paste the full redirected URL or
just the `code` value when prompted — both work.

## Token lifetime and rotation

Strava access tokens are short-lived (on the order of a few hours). Before
any real API request, the CLI checks the stored expiry and refreshes
automatically using the refresh token — you don't need to run `auth` again
for routine use. A successful token response includes a refresh token that
may be the same or a newly issued value; the response is authoritative. The
CLI atomically rewrites `.env` with the returned access token, refresh token,
and expiry each time. If Strava returns a newer refresh token, do not reuse
an older value from a backup.

## Secrets

- Keep `.env` at `chmod 600` on shared machines.
- Never commit, paste, or share `.env`, the client secret, access/refresh
  tokens, or an OAuth authorization code.
- Process environment variables can supply/override credential values, and
  credentials exist in process memory/environment while the CLI runs.
- `.env` and `upload-state.sqlite3` are already excluded from version control
  by this project's `.gitignore`.
- If the client secret may have been exposed, regenerate it from
  <https://www.strava.com/settings/api> and update `.env`.

## Safe first upload: `--limit 1`

Always confirm behavior with at most one actionable candidate before
anything larger:

```bash
python3 -m adidas_to_strava upload ./output --dry-run --limit 1 --verbose
python3 -m adidas_to_strava upload ./output --limit 1 --verbose
```

Check any resulting activity on strava.com before dropping `--limit`.
`upload --dry-run` parses the manifest and checks that each selected file is
parseable XML containing a Running activity, an activity start, and at least
one trackpoint. It is not full TCX-schema validation. The command makes zero
Strava requests, refreshes no token, and writes no upload state.

## How an upload actually happens

Strava's upload endpoint is asynchronous: the CLI `POST`s the TCX file, gets
back an upload ID, then polls that upload's status roughly every 3 seconds
(up to 100 polls, so a few minutes at most) until Strava reports a final
activity ID or a terminal error. A typical upload therefore costs **at least
two API requests** — one submission and one status poll — and can cost more
if Strava takes longer to finish processing the file.

## Local state: `upload-state.sqlite3`

`output/upload-state.sqlite3` is a local SQLite database, one row per
canonical adidas `session_id`, tracking:

- lifecycle `status` (`submitting` → `submitted` → `processing` →
  `completed`/`failed`)
- attempt/submission/completion timestamps
- Strava upload ID and, once known, Strava activity ID
- the latest error or warning text

This is what makes reruns safe: a session already `completed` is skipped, and
a session that was `submitted`/`processing` when the CLI was interrupted
resumes polling on the next run instead of submitting again.

## Duplicate reconciliation

If Strava reports a duplicate and its error text contains a recognizable
existing activity ID, the CLI records that session as `completed` against
that activity without an extra confirmation request. A later rerun then
skips it. If the error does not contain an ID in the format the CLI
recognizes, the upload remains visibly failed/resumable in local state rather
than being silently marked complete.

## `--force`

`--force` bypasses the "already completed" skip and resubmits the session to
Strava, even if local state (or a detected duplicate) says it's done. **This
is the one flag that can create a genuine duplicate activity on Strava.**
Only use it after checking `upload-state.sqlite3` and the corresponding
Strava activity, and only when you're sure a resubmission is what you want.

## Rate limits

The CLI reads Strava's `X-RateLimit-Limit`/`X-RateLimit-Usage` (overall) and
`X-ReadRateLimit-Limit`/`X-ReadRateLimit-Usage` (read) response headers on
every request, keeps a conservative safety reserve, and stops with a rate
limit error *before* actually exhausting either window. An HTTP `429`
response stops immediately regardless of the reserve. A short-window stop
resumes at the next 15-minute boundary; a daily stop requires waiting until
midnight UTC — either way, rerunning the same `upload` command later resumes
safely from local state.

Strava's exact numeric limits depend on your application and subscription,
and they can change — treat the live response headers as authoritative, not
any hardcoded number, and check
<https://developers.strava.com/docs/getting-started/> for current published
defaults.

Timeouts, connection failures, and a handful of transient server error codes
are retried with bounded exponential backoff; permanent client errors are
not retried.

## Cleanup and revocation

When the migration is done:

- Revoke this app's access from Strava's connected-apps settings on your
  account, or
- Delete the private API application itself from
  <https://www.strava.com/settings/api> if you don't need it again, and
- Delete the local `.env` and `output/upload-state.sqlite3` if you no longer
  need to resume or re-authorize.
