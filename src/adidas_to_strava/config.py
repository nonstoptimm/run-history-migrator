"""Local configuration loading and safe Strava token persistence."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class StravaSettings:
    """Credentials and token state for one private Strava application."""

    client_id: str
    client_secret: str
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    env_path: Path


@dataclass(frozen=True)
class TokenBundle:
    """OAuth tokens returned by Strava."""

    access_token: str
    refresh_token: str
    expires_at: int
    scope: str = ""


def default_env_path() -> Path:
    """Return the configured dotenv path or `.env` in the current directory."""
    configured = os.environ.get("ADIDAS_TO_STRAVA_ENV_FILE")
    return Path(configured).expanduser() if configured else Path.cwd() / ".env"


def load_strava_settings(
    env_path: Path | None = None,
    *,
    require_client_secret: bool = True,
) -> StravaSettings:
    """Load Strava configuration from dotenv and the process environment."""
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
    """Atomically update token keys in the ignored local dotenv file."""
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
