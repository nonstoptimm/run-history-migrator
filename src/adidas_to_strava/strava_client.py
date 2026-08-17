"""Isolated HTTP client for Strava OAuth and asynchronous activity uploads."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import StravaSettings, TokenBundle, persist_tokens

LOGGER = logging.getLogger(__name__)
API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = f"{API_BASE}/oauth/token"


class StravaError(RuntimeError):
    """Base error for safe user-facing Strava failures."""


class StravaAuthError(StravaError):
    """Authentication or token refresh failed."""


class StravaAPIError(StravaError):
    """A permanent or exhausted Strava API request failed."""


class StravaRateLimitError(StravaAPIError):
    """Strava rate-limit capacity is exhausted or too close to exhaustion."""


@dataclass(frozen=True)
class RateLimit:
    short_limit: int
    daily_limit: int
    short_usage: int
    daily_usage: int

    @property
    def short_remaining(self) -> int:
        return self.short_limit - self.short_usage

    @property
    def daily_remaining(self) -> int:
        return self.daily_limit - self.daily_usage


@dataclass(frozen=True)
class UploadStatus:
    upload_id: int
    status: str
    error: str | None
    activity_id: int | None


class StravaClient:
    """Strava API client with token refresh, bounded retries, and rate safety."""

    def __init__(
        self,
        settings: StravaSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        max_retries: int = 3,
        timeout: tuple[float, float] = (10.0, 60.0),
        short_reserve: int = 5,
        daily_reserve: int = 20,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self.now = now
        self.max_retries = max_retries
        self.timeout = timeout
        self.short_reserve = short_reserve
        self.daily_reserve = daily_reserve
        self.overall_rate_limit: RateLimit | None = None
        self.read_rate_limit: RateLimit | None = None

    def authorization_token(self) -> str:
        """Return a valid access token, refreshing and persisting when needed."""
        expires_at = self.settings.token_expires_at or 0
        if self.settings.access_token and expires_at > int(self.now()) + 300:
            return self.settings.access_token
        if not self.settings.refresh_token:
            raise StravaAuthError("Strava authorization is required; run the auth command")
        tokens = self.refresh_access_token(self.settings.refresh_token)
        self._apply_tokens(tokens)
        return tokens.access_token

    def exchange_authorization_code(self, code: str) -> TokenBundle:
        """Exchange a one-use OAuth authorization code for token credentials."""
        payload = self._token_request(
            {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            }
        )
        return self._parse_tokens(payload)

    def persist_token_bundle(self, tokens: TokenBundle) -> None:
        """Persist a validated token response and use it for future requests."""
        self._apply_tokens(tokens)

    def refresh_access_token(self, refresh_token: str) -> TokenBundle:
        """Refresh an expired access token using Strava's rotating token."""
        payload = self._token_request(
            {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        return self._parse_tokens(payload)

    def submit_upload(
        self,
        tcx_path: Path,
        *,
        external_id: str,
    ) -> UploadStatus:
        """Submit one TCX file and return Strava's asynchronous upload record."""
        payload = self._request_json(
            "POST",
            f"{API_BASE}/uploads",
            data={
                "data_type": "tcx",
                "activity_type": "run",
                "external_id": external_id,
            },
            file_path=tcx_path,
        )
        return self._parse_upload_status(payload)

    def get_upload_status(self, upload_id: int) -> UploadStatus:
        """Retrieve the current asynchronous upload-processing state."""
        payload = self._request_json("GET", f"{API_BASE}/uploads/{upload_id}")
        return self._parse_upload_status(payload)

    def _apply_tokens(self, tokens: TokenBundle) -> None:
        persist_tokens(self.settings.env_path, tokens)
        self.settings = StravaSettings(
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_expires_at=tokens.expires_at,
            env_path=self.settings.env_path,
        )

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        transient_statuses = {408, 425, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(TOKEN_URL, data=data, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise StravaAuthError(
                        f"Could not contact Strava for OAuth tokens after {attempt + 1} attempts"
                    ) from exc
                self._backoff(attempt)
                continue
            if response.status_code == 429:
                raise StravaAuthError("Strava OAuth is rate limited; retry after the limit resets")
            if response.status_code in transient_statuses:
                if attempt >= self.max_retries:
                    raise StravaAuthError(
                        f"Strava OAuth returned HTTP {response.status_code} after bounded retries"
                    )
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise StravaAuthError(
                    f"Strava OAuth failed with HTTP {response.status_code}; "
                    "credentials were not changed"
                )
            try:
                return self._response_json(response, "Strava OAuth")
            except StravaAPIError as exc:
                raise StravaAuthError(str(exc)) from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _parse_tokens(payload: dict[str, Any]) -> TokenBundle:
        try:
            return TokenBundle(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=int(payload["expires_at"]),
                scope=str(payload.get("scope", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StravaAuthError("Strava returned an incomplete OAuth token response") from exc

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        file_path: Path | None = None,
    ) -> dict[str, Any]:
        token = self.authorization_token()
        transient_statuses = {408, 425, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            is_upload_submission = method == "POST" and url.rstrip("/").endswith("/uploads")
            self._ensure_rate_capacity(include_read=not is_upload_submission)
            try:
                if file_path:
                    with file_path.open("rb") as handle:
                        response = self.session.request(
                            method,
                            url,
                            headers={"Authorization": f"Bearer {token}"},
                            data=data,
                            files={"file": (file_path.name, handle, "application/xml")},
                            timeout=self.timeout,
                        )
                else:
                    response = self.session.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data=data,
                        timeout=self.timeout,
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise StravaAPIError(
                        f"Strava request failed after {attempt + 1} attempts"
                    ) from exc
                self._backoff(attempt)
                continue
            self._capture_rate_limits(response)
            if response.status_code == 429:
                raise StravaRateLimitError(
                    "Strava returned HTTP 429; stop and resume after the rate limit resets"
                )
            if response.status_code in transient_statuses:
                if attempt >= self.max_retries:
                    raise StravaAPIError(
                        f"Strava returned HTTP {response.status_code} after bounded retries"
                    )
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                detail = self._safe_error_detail(response)
                suffix = f": {detail}" if detail else ""
                raise StravaAPIError(f"Strava returned HTTP {response.status_code}{suffix}")
            return self._response_json(response, "Strava API")
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> None:
        self.sleep(min(2**attempt + random.random(), 15.0))

    def _ensure_rate_capacity(self, *, include_read: bool) -> None:
        limits = [("overall", self.overall_rate_limit)]
        if include_read:
            limits.append(("read", self.read_rate_limit))
        for label, limit in limits:
            if not limit:
                continue
            if limit.daily_remaining <= self.daily_reserve:
                midnight = datetime.fromtimestamp(self.now(), tz=UTC).date() + timedelta(days=1)
                raise StravaRateLimitError(
                    f"Strava {label} daily rate limit is nearly exhausted; "
                    f"resume after {midnight.isoformat()}T00:00:00+00:00"
                )
            if limit.short_remaining <= self.short_reserve:
                reset_epoch = ((int(self.now()) // 900) + 1) * 900
                reset = datetime.fromtimestamp(reset_epoch, tz=UTC).isoformat(timespec="seconds")
                raise StravaRateLimitError(
                    f"Strava {label} 15-minute rate limit is nearly exhausted; resume after {reset}"
                )

    def _capture_rate_limits(self, response: requests.Response) -> None:
        overall = self._parse_rate_limit(
            response.headers.get("X-RateLimit-Limit"),
            response.headers.get("X-RateLimit-Usage"),
        )
        read = self._parse_rate_limit(
            response.headers.get("X-ReadRateLimit-Limit"),
            response.headers.get("X-ReadRateLimit-Usage"),
        )
        if overall:
            self.overall_rate_limit = overall
        if read:
            self.read_rate_limit = read

    @staticmethod
    def _parse_rate_limit(limits: str | None, usage: str | None) -> RateLimit | None:
        if not limits or not usage:
            return None
        try:
            short_limit, daily_limit = (int(value) for value in limits.split(",", 1))
            short_usage, daily_usage = (int(value) for value in usage.split(",", 1))
        except ValueError:
            return None
        return RateLimit(short_limit, daily_limit, short_usage, daily_usage)

    @staticmethod
    def _response_json(response: requests.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise StravaAPIError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise StravaAPIError(f"{label} returned an unexpected response")
        return payload

    @staticmethod
    def _safe_error_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        value = payload.get("message") or payload.get("error")
        return str(value)[:300] if value else ""

    @staticmethod
    def _parse_upload_status(payload: dict[str, Any]) -> UploadStatus:
        try:
            upload_id = int(payload["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StravaAPIError("Strava returned an upload response without an ID") from exc
        activity = payload.get("activity_id")
        return UploadStatus(
            upload_id=upload_id,
            status=str(payload.get("status") or ""),
            error=str(payload["error"]) if payload.get("error") else None,
            activity_id=int(activity) if activity is not None else None,
        )
