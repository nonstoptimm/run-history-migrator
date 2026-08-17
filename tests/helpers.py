from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import IO, NotRequired, TypedDict

import requests


class PostCall(TypedDict):
    url: str
    data: Mapping[str, str] | None
    timeout: tuple[float, float] | None


class RequestCall(TypedDict):
    method: str
    url: str
    headers: Mapping[str, str] | None
    data: Mapping[str, str] | None
    files: NotRequired[Mapping[str, tuple[str, IO[bytes], str]]]
    timeout: tuple[float, float] | None


def make_export(root: Path) -> tuple[Path, Path]:
    export = root / "export"
    sessions = export / "Sport-sessions"
    for directory in ("GPS-data", "Heart-rate-data", "Elevation-data"):
        (sessions / directory).mkdir(parents=True)
    return export, sessions


def write_session(
    sessions: Path,
    session_id: str,
    *,
    start_ms: int = 1_672_531_200_000,
    offset_ms: int = 0,
    sport_id: str = "1",
    malformed: bool = False,
) -> Path:
    path = sessions / f"arbitrary-prefix_{session_id}.json"
    if malformed:
        path.write_text("{", encoding="utf-8")
        return path
    data: dict[str, object] = {
        "id": session_id,
        "start_time": start_ms,
        "start_time_timezone_offset": offset_ms,
        "end_time": start_ms + 10_000,
        "duration": 10_000,
        "pause": 0,
        "calories": 100,
        "sport_type_id": sport_id,
        "features": [
            {"type": "track_metrics", "attributes": {"distance": 1000}},
            {
                "type": "initial_values",
                "attributes": {
                    "sport_type": {"id": sport_id},
                    "distance": 1000,
                    "duration": 10_000,
                    "start_time": start_ms,
                },
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_gps(
    sessions: Path,
    session_id: str,
    *,
    start_ms: int = 1_672_531_200_000,
) -> Path:
    rows: list[dict[str, int | float]] = [
        {
            "timestamp": start_ms,
            "latitude": 48.1,
            "longitude": 11.5,
            "altitude": 500,
            "distance": 0,
        },
        {
            "timestamp": start_ms + 10_000,
            "latitude": 48.2,
            "longitude": 11.6,
            "altitude": 501,
            "distance": 1000,
        },
    ]
    path = sessions / "GPS-data" / f"wrong-prefix_{session_id}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload: object = payload
        self._headers = headers or {}

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    def json(self) -> object:
        if isinstance(self.payload, ValueError):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(
        self,
        *,
        posts: list[FakeResponse | requests.RequestException] | None = None,
        requests_: list[FakeResponse | requests.RequestException] | None = None,
    ) -> None:
        self.posts: list[FakeResponse | requests.RequestException] = list(posts or [])
        self.requests: list[FakeResponse | requests.RequestException] = list(requests_ or [])
        self.post_calls: list[PostCall] = []
        self.request_calls: list[RequestCall] = []

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> FakeResponse:
        self.post_calls.append({"url": url, "data": data, "timeout": timeout})
        response = self.posts.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return response

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, IO[bytes], str]] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> FakeResponse:
        call: RequestCall = {
            "method": method,
            "url": url,
            "headers": headers,
            "data": data,
            "timeout": timeout,
        }
        if files is not None:
            call["files"] = files
        self.request_calls.append(call)
        response = self.requests.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return response
