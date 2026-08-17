"""Isolated HTTP client for Strava OAuth and asynchronous activity uploads."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Protocol

import requests

from .config import StravaSettings, TokenBundle, persist_tokens

LOGGER = logging.getLogger(__name__)
API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = f"{API_BASE}/oauth/token"

type JsonMapping = Mapping[str, object]
"""A decoded JSON object, kept intentionally shallow at the HTTP boundary."""


class HTTPResponse(Protocol):
    """Structural shape of an HTTP response, satisfied by `requests.Response`.

    Only the members `StravaClient` actually reads are declared, so
    lightweight test doubles can conform without inheriting from
    `requests.Response`.
    """

    status_code: int

    @property
    def headers(self) -> Mapping[str, str]:
        """Response headers, used to read Strava's rate-limit counters."""
        ...

    def json(self) -> object:
        """Decode the response body as JSON.

        Returns:
            The decoded JSON value. Strava always returns a JSON object for
            the endpoints this client calls.

        Raises:
            ValueError: If the response body is not valid JSON.
        """
        ...


class HTTPSession(Protocol):
    """Structural shape of the HTTP session used for Strava requests.

    `requests.Session` satisfies this protocol directly; tests may supply a
    minimal fake implementing only `post` and `request`.
    """

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HTTPResponse:
        """Issue an HTTP POST request, used only for OAuth token exchange."""
        ...

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, IO[bytes], str]] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HTTPResponse:
        """Issue an HTTP request with an explicit method, used for the API."""
        ...


class StravaError(RuntimeError):
    """Base error for safe user-facing Strava failures."""


class StravaAuthError(StravaError):
    """Authentication or token refresh failed."""


class StravaAPIError(StravaError):
    """A permanent or exhausted Strava API request failed."""


class StravaRateLimitError(StravaAPIError):
    """Strava rate-limit capacity is exhausted or too close to exhaustion."""


def _coerce_int(value: object) -> int:
    """Coerce one decoded JSON value to `int`, matching plain `int()` rules.

    Args:
        value: A JSON-decoded value expected to represent an integer, such
            as Strava's numeric or numeric-string IDs.

    Returns:
        The coerced integer.

    Raises:
        TypeError: If `value` is not an `int`, `float`, `bool`, or `str`.
        ValueError: If `value` is a `str` that does not represent an int.
    """
    if isinstance(value, int | float | str):
        return int(value)
    raise TypeError(f"expected an int-like value, got {type(value).__name__}")


@dataclass(frozen=True)
class RateLimit:
    """Strava's usage counters for one rate-limit tier (overall or read).

    Attributes:
        short_limit: Maximum requests allowed in the current 15-minute
            window.
        daily_limit: Maximum requests allowed in the current UTC day.
        short_usage: Requests already used in the current 15-minute window.
        daily_usage: Requests already used in the current UTC day.
    """

    short_limit: int
    daily_limit: int
    short_usage: int
    daily_usage: int

    @property
    def short_remaining(self) -> int:
        """Return the number of requests left in the current 15-minute window."""
        return self.short_limit - self.short_usage

    @property
    def daily_remaining(self) -> int:
        """Return the number of requests left in the current UTC day."""
        return self.daily_limit - self.daily_usage


@dataclass(frozen=True)
class UploadStatus:
    """Strava's asynchronous processing state for one submitted upload.

    Attributes:
        upload_id: Strava's identifier for the upload job.
        status: Strava's human-readable processing status (for example
            ``"processing"`` or ``"ready"``).
        error: Strava's error message, or ``None`` while there is no error.
            A duplicate-upload error still reports the existing activity in
            the message text; see `activity_id`.
        activity_id: The created (or pre-existing, for duplicates) activity
            ID once known, or ``None`` while the upload is still processing.
    """

    upload_id: int
    status: str
    error: str | None
    activity_id: int | None


class StravaClient:
    """Strava API client with token refresh, bounded retries, and rate safety.

    Attributes:
        settings: The current credentials and token state; replaced in place
            whenever tokens are refreshed.
        session: The HTTP session used for all requests.
        sleep: The delay function used between retries; injectable for
            tests.
        now: The clock function used for token-expiry and rate-limit
            calculations; injectable for tests.
        max_retries: The number of retries allowed for transient failures
            before an operation gives up and raises.
        timeout: The ``(connect, read)`` timeout, in seconds, for one HTTP
            request attempt.
        short_reserve: The number of requests to keep unused in the current
            15-minute window before refusing further requests.
        daily_reserve: The number of requests to keep unused in the current
            UTC day before refusing further requests.
        overall_rate_limit: The most recently observed overall rate-limit
            counters, or ``None`` before any response has reported them.
        read_rate_limit: The most recently observed read-only rate-limit
            counters, or ``None`` before any response has reported them.
    """

    def __init__(
        self,
        settings: StravaSettings,
        *,
        session: HTTPSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        max_retries: int = 3,
        timeout: tuple[float, float] = (10.0, 60.0),
        short_reserve: int = 5,
        daily_reserve: int = 20,
    ) -> None:
        """Initialize the client.

        Args:
            settings: Credentials and any cached tokens to start from.
            session: The HTTP session to issue requests with; defaults to a
                new `requests.Session`. Tests may pass any object that
                conforms to `HTTPSession`.
            sleep: The delay function called between bounded retries.
            now: The clock function used to decide token and rate-limit
                freshness.
            max_retries: The number of retries allowed for transient
                failures (timeouts, connection errors, and HTTP 408/425/
                500/502/503/504) before raising.
            timeout: The ``(connect, read)`` timeout, in seconds, applied to
                every HTTP request attempt.
            short_reserve: The number of requests to keep unused in the
                current 15-minute window; requests stop early once
                remaining capacity reaches this reserve.
            daily_reserve: The number of requests to keep unused in the
                current UTC day; requests stop early once remaining
                capacity reaches this reserve.
        """
        self.settings = settings
        self.session: HTTPSession = session or requests.Session()
        self.sleep = sleep
        self.now = now
        self.max_retries = max_retries
        self.timeout = timeout
        self.short_reserve = short_reserve
        self.daily_reserve = daily_reserve
        self.overall_rate_limit: RateLimit | None = None
        self.read_rate_limit: RateLimit | None = None

    def authorization_token(self) -> str:
        """Return a valid access token, refreshing and persisting when needed.

        A cached access token is reused as-is whenever it still has more
        than five minutes of validity left, avoiding an unnecessary refresh
        request. Otherwise the refresh token is exchanged for a new token
        bundle, which is persisted before being returned.

        Returns:
            A currently valid Strava access token.

        Raises:
            StravaAuthError: If no refresh token is available, or if the
                refresh request fails.
        """
        expires_at = self.settings.token_expires_at or 0
        if self.settings.access_token and expires_at > int(self.now()) + 300:
            return self.settings.access_token
        if not self.settings.refresh_token:
            raise StravaAuthError("Strava authorization is required; run the auth command")
        tokens = self.refresh_access_token(self.settings.refresh_token)
        self._apply_tokens(tokens)
        return tokens.access_token

    def exchange_authorization_code(self, code: str) -> TokenBundle:
        """Exchange a one-use OAuth authorization code for token credentials.

        Args:
            code: The one-use authorization code returned by Strava's
                OAuth redirect or pasted in manually.

        Returns:
            The token bundle Strava issued; not yet persisted.

        Raises:
            StravaAuthError: If the code has already been used, is invalid,
                or the request fails after bounded retries.
        """
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
        """Persist a validated token response and use it for future requests.

        Args:
            tokens: The token bundle to persist, replacing any cached
                tokens for subsequent requests made by this client.
        """
        self._apply_tokens(tokens)

    def refresh_access_token(self, refresh_token: str) -> TokenBundle:
        """Refresh an expired access token using Strava's rotating token.

        Args:
            refresh_token: The refresh token to exchange. Strava rotates
                refresh tokens, so the bundle returned here supersedes it.

        Returns:
            The token bundle Strava issued; not yet persisted.

        Raises:
            StravaAuthError: If the refresh token is invalid, or the
                request fails after bounded retries.
        """
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
        """Submit one TCX file and return Strava's asynchronous upload record.

        Strava processes uploads asynchronously; the returned status often
        still shows the upload as processing, with no activity ID yet. Poll
        `get_upload_status` with the returned `UploadStatus.upload_id` until
        processing finishes.

        Args:
            tcx_path: The TCX file to upload.
            external_id: A caller-chosen identifier Strava echoes back,
                used here to make re-submission of the same local activity
                recognizable in Strava's duplicate-detection error text.

        Returns:
            The initial upload status, which may already report a
            duplicate error or a completed activity ID.

        Raises:
            StravaAPIError: If the request fails after bounded retries.
            StravaRateLimitError: If Strava's rate limit is exhausted or
                reserved capacity would be exceeded.
        """
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
        """Retrieve the current asynchronous upload-processing state.

        Args:
            upload_id: The Strava upload ID returned by `submit_upload`.

        Returns:
            The current processing status for the upload.

        Raises:
            StravaAPIError: If the request fails after bounded retries.
            StravaRateLimitError: If Strava's rate limit is exhausted or
                reserved capacity would be exceeded.
        """
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

    def _token_request(self, data: dict[str, str]) -> JsonMapping:
        """Send one OAuth token request, retrying bounded transient failures.

        Network errors and HTTP 408/425/500/502/503/504 responses are
        retried with backoff up to `max_retries` times; any other failure,
        including HTTP 429, stops immediately without retrying.

        Raises:
            StravaAuthError: If Strava is rate limited (HTTP 429), returns
                a non-retryable error, or retries are exhausted.
        """
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
    def _parse_tokens(payload: JsonMapping) -> TokenBundle:
        try:
            return TokenBundle(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=_coerce_int(payload["expires_at"]),
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
    ) -> JsonMapping:
        """Send one authorized API request, retrying bounded transient failures.

        Rate-limit capacity is checked before every attempt (see
        `_ensure_rate_capacity`), and observed rate-limit headers are
        captured after every response. Network errors and HTTP 408/425/500/
        502/503/504 responses are retried with backoff up to `max_retries`
        times; HTTP 429 stops immediately, since Strava's rate limit is
        already exhausted and retrying would not help.

        Raises:
            StravaAPIError: If the request fails after bounded retries or
                Strava returns another non-retryable error.
            StravaRateLimitError: If Strava returns HTTP 429, or reserved
                capacity from `_ensure_rate_capacity` would be exceeded.
        """
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
        """Raise before a request would push usage past the reserved margin.

        `short_reserve` and `daily_reserve` requests are always kept unused
        so a burst of local requests cannot itself trigger Strava's hard
        rate limit; once remaining capacity reaches a reserve, requests
        stop early with a message naming when the limit resets.

        Args:
            include_read: Whether to also check the read-only rate-limit
                tier, skipped for upload submissions which only consume
                overall (write) capacity.

        Raises:
            StravaRateLimitError: If either checked tier's remaining daily
                or 15-minute capacity is at or below its reserve.
        """
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

    def _capture_rate_limits(self, response: HTTPResponse) -> None:
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
    def _response_json(response: HTTPResponse, label: str) -> JsonMapping:
        try:
            payload = response.json()
        except ValueError as exc:
            raise StravaAPIError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise StravaAPIError(f"{label} returned an unexpected response")
        return payload

    @staticmethod
    def _safe_error_detail(response: HTTPResponse) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        value = payload.get("message") or payload.get("error")
        return str(value)[:300] if value else ""

    @staticmethod
    def _parse_upload_status(payload: JsonMapping) -> UploadStatus:
        try:
            upload_id = _coerce_int(payload["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StravaAPIError("Strava returned an upload response without an ID") from exc
        activity = payload.get("activity_id")
        return UploadStatus(
            upload_id=upload_id,
            status=str(payload.get("status") or ""),
            error=str(payload["error"]) if payload.get("error") else None,
            activity_id=_coerce_int(activity) if activity is not None else None,
        )
