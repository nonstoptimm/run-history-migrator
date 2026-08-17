"""Local configuration loading and safe Strava token persistence."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class StravaSettings:
    """Credentials and token state for one private Strava application.

    Attributes:
        client_id: The Strava API application's client ID.
        client_secret: The Strava API application's client secret.
        access_token: A currently valid access token, or ``None`` if the CLI
            has not yet authorized or the cached token has expired.
        refresh_token: A refresh token usable to obtain a new access token,
            or ``None`` if the CLI has not yet authorized.
        token_expires_at: The Unix timestamp when `access_token` expires, or
            ``None`` if no access token is cached.
        env_path: The resolved dotenv file this configuration was loaded
            from, and where refreshed tokens are persisted.
    """

    client_id: str
    client_secret: str
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    env_path: Path


@dataclass(frozen=True)
class TokenBundle:
    """OAuth tokens returned by Strava.

    Attributes:
        access_token: A short-lived token used to authorize API requests.
        refresh_token: A rotating token used to obtain a new access token.
        expires_at: The Unix timestamp when `access_token` expires.
        scope: The comma- or space-separated scopes Strava granted, or an
            empty string if Strava did not report them.
    """

    access_token: str
    refresh_token: str
    expires_at: int
    scope: str = ""


def default_env_path() -> Path:
    """Return the configured dotenv path or `.env` in the current directory.

    Returns:
        The path from the ``ADIDAS_TO_STRAVA_ENV_FILE`` environment
        variable if set, otherwise ``.env`` in the current working
        directory.
    """
    configured = os.environ.get("ADIDAS_TO_STRAVA_ENV_FILE")
    return Path(configured).expanduser() if configured else Path.cwd() / ".env"


def load_strava_settings(
    env_path: Path | None = None,
    *,
    require_client_secret: bool = True,
) -> StravaSettings:
    """Load Strava configuration from dotenv and the process environment.

    Existing process environment variables take precedence over the dotenv
    file, and the dotenv file itself is never modified by this function.

    Args:
        env_path: The dotenv file to load, or `default_env_path` if omitted.
        require_client_secret: Whether a missing client secret should raise.
            Set to ``False`` for read-only flows that only need the client
            ID (for example, printing the authorization URL).

    Returns:
        The loaded settings, including any cached tokens.

    Raises:
        ValueError: If `client_id` is missing, if `client_secret` is missing
            and `require_client_secret` is ``True``, or if
            ``STRAVA_TOKEN_EXPIRES_AT`` is set but is not a valid integer.
    """
    path = (env_path or default_env_path()).expanduser().resolve()
    load_dotenv(path, override=False)
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id:
        raise ValueError(f"STRAVA_CLIENT_ID is required; configure {path}")
    if require_client_secret and not client_secret:
        raise ValueError(f"STRAVA_CLIENT_SECRET is required; configure {path}")
    expires_raw = os.environ.get("STRAVA_TOKEN_EXPIRES_AT", "").strip()
    try:
        expires_at = int(expires_raw) if expires_raw else None
    except ValueError as exc:
        raise ValueError("STRAVA_TOKEN_EXPIRES_AT must be a Unix timestamp") from exc
    return StravaSettings(
        client_id=client_id,
        client_secret=client_secret,
        access_token=os.environ.get("STRAVA_ACCESS_TOKEN") or None,
        refresh_token=os.environ.get("STRAVA_REFRESH_TOKEN") or None,
        token_expires_at=expires_at,
        env_path=path,
    )


def _dotenv_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def persist_tokens(env_path: Path, tokens: TokenBundle) -> None:
    """Atomically update token keys in the ignored local dotenv file.

    Reused and refreshed tokens are the only values ever rewritten: existing
    lines for other keys are preserved verbatim, and the update is written
    to a sibling temporary file and swapped into place with `os.replace` so
    a crash mid-write cannot leave a truncated or empty file. The written
    file is also made owner-readable only (mode ``0o600``). After the
    dotenv file is updated, the current process environment is updated too,
    so a client already holding these settings can keep using fresh tokens.

    Args:
        env_path: The dotenv file to update, created if it does not exist.
        tokens: The token bundle to persist, replacing any previous tokens.

    Raises:
        OSError: If the temporary file cannot be written or swapped into
            place.
    """
    path = env_path.expanduser().resolve()
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {
        "STRAVA_ACCESS_TOKEN": tokens.access_token,
        "STRAVA_REFRESH_TOKEN": tokens.refresh_token,
        "STRAVA_TOKEN_EXPIRES_AT": str(tokens.expires_at),
    }
    written: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        stripped = line.lstrip()
        key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else ""
        if key in replacements:
            output.append(f"{key}={_dotenv_value(replacements[key])}")
            written.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in written:
            output.append(f"{key}={_dotenv_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    os.environ["STRAVA_ACCESS_TOKEN"] = tokens.access_token
    os.environ["STRAVA_REFRESH_TOKEN"] = tokens.refresh_token
    os.environ["STRAVA_TOKEN_EXPIRES_AT"] = str(tokens.expires_at)
