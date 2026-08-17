from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict, Unpack
from urllib.parse import parse_qs, urlparse

import pytest

from adidas_to_strava.config import StravaSettings, load_strava_settings
from adidas_to_strava.strava_auth import authorize, build_authorization_url
from adidas_to_strava.strava_client import StravaAuthError, StravaClient

from .helpers import FakeResponse, FakeSession


class SettingsOverrides(TypedDict, total=False):
    client_id: str
    client_secret: str
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    env_path: Path


def settings(tmp_path: Path, **overrides: Unpack[SettingsOverrides]) -> StravaSettings:
    return StravaSettings(
        client_id=overrides.get("client_id", "1234"),
        client_secret=overrides.get("client_secret", "client-secret-value"),
        access_token=overrides.get("access_token"),
        refresh_token=overrides.get("refresh_token"),
        token_expires_at=overrides.get("token_expires_at"),
        env_path=overrides.get("env_path", tmp_path / ".env"),
    )


def test_authorization_url_contains_required_scopes_and_state() -> None:
    url = build_authorization_url("1234", "http://localhost:8765/callback", "state-value")
    query = parse_qs(urlparse(url).query)
    assert query["state"] == ["state-value"]
    assert set(query["scope"][0].split(",")) == {"read", "activity:write"}
    assert query["response_type"] == ["code"]


def test_token_exchange_and_persistence(tmp_path: Path) -> None:
    fake = FakeSession(
        posts=[
            FakeResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_at": 2_000,
                    "scope": "read activity:write",
                },
            )
        ]
    )
    client = StravaClient(settings(tmp_path), session=fake)
    tokens = client.exchange_authorization_code("one-use-code")
    client.persist_token_bundle(tokens)
    contents = (tmp_path / ".env").read_text()
    assert "new-access" in contents
    assert "new-refresh" in contents
    assert "one-use-code" not in contents


def test_missing_required_scope_is_rejected_before_persistence(tmp_path: Path) -> None:
    fake = FakeSession(
        posts=[
            FakeResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_at": 2_000,
                    "scope": "read",
                },
            )
        ]
    )
    client = StravaClient(settings(tmp_path), session=fake)
    with pytest.raises(StravaAuthError, match="activity:write"):
        authorize(settings(tmp_path), client, code="one-use-code")
    assert not (tmp_path / ".env").exists()


def test_valid_token_reused_without_request(tmp_path: Path) -> None:
    fake = FakeSession()
    client = StravaClient(
        settings(tmp_path, access_token="valid", token_expires_at=10_000),
        session=fake,
        now=lambda: 1_000,
    )
    assert client.authorization_token() == "valid"
    assert fake.post_calls == []


def test_expired_token_refreshes_and_rotates(tmp_path: Path) -> None:
    fake = FakeSession(
        posts=[
            FakeResponse(
                200,
                {
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_at": 20_000,
                },
            )
        ]
    )
    client = StravaClient(
        settings(
            tmp_path,
            access_token="expired",
            refresh_token="old-refresh",
            token_expires_at=1,
        ),
        session=fake,
        now=lambda: 10_000,
    )
    assert client.authorization_token() == "rotated-access"
    assert "rotated-refresh" in (tmp_path / ".env").read_text()


def test_token_refresh_retries_transient_server_failure(tmp_path: Path) -> None:
    sleeps: list[float] = []
    fake = FakeSession(
        posts=[
            FakeResponse(503, {"message": "temporary"}),
            FakeResponse(
                200,
                {
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_at": 20_000,
                },
            ),
        ]
    )
    client = StravaClient(
        settings(tmp_path, refresh_token="old-refresh", token_expires_at=1),
        session=fake,
        now=lambda: 10_000,
        sleep=sleeps.append,
    )
    assert client.authorization_token() == "rotated-access"
    assert len(sleeps) == 1


def test_refresh_failure_does_not_log_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeSession(posts=[FakeResponse(401, {"message": "bad credentials"})])
    secret = "never-log-this-secret"
    client = StravaClient(
        settings(
            tmp_path,
            client_secret=secret,
            refresh_token="private-refresh",
            token_expires_at=1,
        ),
        session=fake,
        now=lambda: 10_000,
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(StravaAuthError):
        client.authorization_token()
    assert secret not in caplog.text
    assert "private-refresh" not in caplog.text


def test_load_settings_validates_required_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_ACCESS_TOKEN",
        "STRAVA_REFRESH_TOKEN",
        "STRAVA_TOKEN_EXPIRES_AT",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="STRAVA_CLIENT_ID"):
        load_strava_settings(tmp_path / ".env")
