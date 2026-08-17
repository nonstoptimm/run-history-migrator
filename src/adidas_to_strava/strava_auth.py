"""Personal localhost OAuth flow for the Strava CLI."""

from __future__ import annotations

import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .config import StravaSettings, TokenBundle
from .strava_client import StravaAuthError, StravaClient

AUTHORIZATION_URL = "https://www.strava.com/oauth/authorize"
REQUIRED_SCOPES = frozenset({"read", "activity:write"})


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build the official Strava authorization URL with required scopes."""
    return f"{AUTHORIZATION_URL}?{
        urlencode(
            {
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'response_type': 'code',
                'approval_prompt': 'auto',
                'scope': ','.join(sorted(REQUIRED_SCOPES)),
                'state': state,
            }
        )
    }"


def _validate_scopes(scope: str) -> None:
    granted = {value for value in scope.replace(",", " ").split() if value}
    missing = REQUIRED_SCOPES - granted
    if missing:
        raise StravaAuthError(
            f"Strava authorization is missing required scope(s): {', '.join(sorted(missing))}"
        )


def _validate_exchange_scope(tokens: TokenBundle, supplied_scope: str = "") -> None:
    scope = tokens.scope or supplied_scope
    if not scope:
        raise StravaAuthError("Strava did not return granted scopes; authorization was not saved")
    _validate_scopes(scope)


def authorize(
    settings: StravaSettings,
    client: StravaClient,
    *,
    code: str | None = None,
    granted_scope: str = "",
    port: int = 8765,
    timeout: int = 180,
    open_browser: bool = True,
    input_func=input,
) -> TokenBundle:
    """Authorize interactively, using localhost callback and manual fallback."""
    if code:
        tokens = client.exchange_authorization_code(code)
        _validate_exchange_scope(tokens, granted_scope)
        client.persist_token_bundle(tokens)
        return tokens

    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}/callback"
    authorization_url = build_authorization_url(settings.client_id, redirect_uri, state)
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            result["state"] = query.get("state", [""])[0]
            result["code"] = query.get("code", [""])[0]
            result["scope"] = query.get("scope", [""])[0]
            result["error"] = query.get("error", [""])[0]
            body = b"Authorization received. You may close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    print("Open this URL to authorize the CLI:")
    print(authorization_url)
    server: HTTPServer | None = None
    try:
        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        server.timeout = timeout
        if open_browser:
            webbrowser.open(authorization_url)
        server.handle_request()
    except OSError:
        result = {}
    finally:
        if server:
            server.server_close()

    if result.get("error"):
        raise StravaAuthError(f"Strava authorization was denied: {result['error']}")
    callback_code = result.get("code", "")
    if callback_code:
        if result.get("state") != state:
            raise StravaAuthError("OAuth callback state did not match")
        _validate_scopes(result.get("scope", ""))
        tokens = client.exchange_authorization_code(callback_code)
        client.persist_token_bundle(tokens)
        return tokens

    manual = input_func("Paste the authorization code or callback URL: ").strip()
    manual_scope = ""
    if "://" in manual:
        query = parse_qs(urlparse(manual).query)
        manual_code = query.get("code", [""])[0]
        manual_scope = query.get("scope", [""])[0]
        manual_state = query.get("state", [""])[0]
        if manual_state and manual_state != state:
            raise StravaAuthError("OAuth callback state did not match")
        if manual_scope:
            _validate_scopes(manual_scope)
    else:
        manual_code = manual
    if not manual_code:
        raise StravaAuthError("No authorization code was provided")
    tokens = client.exchange_authorization_code(manual_code)
    _validate_exchange_scope(tokens, manual_scope)
    client.persist_token_bundle(tokens)
    return tokens


def credentials_location(settings: StravaSettings) -> Path:
    """Return the local ignored file where refreshed credentials are stored."""
    return settings.env_path
