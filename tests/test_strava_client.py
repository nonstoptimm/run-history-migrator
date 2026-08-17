from __future__ import annotations

import pytest
import requests

from adidas_to_strava.config import StravaSettings
from adidas_to_strava.strava_client import (
    StravaAPIError,
    StravaClient,
    StravaRateLimitError,
)

from .helpers import FakeResponse, FakeSession


def settings(tmp_path) -> StravaSettings:
    return StravaSettings(
        client_id="123",
        client_secret="secret",
        access_token="access",
        refresh_token="refresh",
        token_expires_at=99_999,
        env_path=tmp_path / ".env",
    )


def test_submit_and_poll_upload(tmp_path) -> None:
    tcx = tmp_path / "run.tcx"
    tcx.write_text("<tcx/>", encoding="utf-8")
    fake = FakeSession(
        requests_=[
            FakeResponse(201, {"id": 10, "status": "processing", "activity_id": None}),
            FakeResponse(200, {"id": 10, "status": "ready", "activity_id": 99}),
        ]
    )
    client = StravaClient(settings(tmp_path), session=fake, now=lambda: 1_000)
    submitted = client.submit_upload(tcx, external_id="adidas-id.tcx")
    completed = client.get_upload_status(10)
    assert submitted.upload_id == 10
    assert completed.activity_id == 99
    assert fake.request_calls[0]["data"]["data_type"] == "tcx"
    assert "name" not in fake.request_calls[0]["data"]


def test_transient_error_and_timeout_retry_are_bounded(tmp_path) -> None:
    sleeps = []
    fake = FakeSession(
        requests_=[
            requests.Timeout(),
            FakeResponse(503, {"message": "temporary"}),
            FakeResponse(200, {"id": 10, "status": "ready", "activity_id": 99}),
        ]
    )
    client = StravaClient(
        settings(tmp_path),
        session=fake,
        now=lambda: 1_000,
        sleep=sleeps.append,
        max_retries=2,
    )
    assert client.get_upload_status(10).activity_id == 99
    assert len(sleeps) == 2
    assert len(fake.request_calls) == 3


def test_permanent_failure_is_not_retried(tmp_path) -> None:
    fake = FakeSession(requests_=[FakeResponse(400, {"message": "bad upload"})])
    client = StravaClient(settings(tmp_path), session=fake, now=lambda: 1_000)
    with pytest.raises(StravaAPIError, match="bad upload"):
        client.get_upload_status(10)
    assert len(fake.request_calls) == 1


def test_http_429_stops_immediately(tmp_path) -> None:
    fake = FakeSession(requests_=[FakeResponse(429, {"message": "rate limit"})])
    client = StravaClient(settings(tmp_path), session=fake, now=lambda: 1_000)
    with pytest.raises(StravaRateLimitError):
        client.get_upload_status(10)
    assert len(fake.request_calls) == 1


def test_rate_limit_headers_stop_before_next_request(tmp_path) -> None:
    fake = FakeSession(
        requests_=[
            FakeResponse(
                200,
                {"id": 10, "status": "processing"},
                {
                    "X-RateLimit-Limit": "200,2000",
                    "X-RateLimit-Usage": "195,100",
                },
            )
        ]
    )
    client = StravaClient(settings(tmp_path), session=fake, now=lambda: 1_000)
    client.get_upload_status(10)
    with pytest.raises(StravaRateLimitError, match="15-minute"):
        client.get_upload_status(10)
    assert len(fake.request_calls) == 1
