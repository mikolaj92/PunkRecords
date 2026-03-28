from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from .models import AccountRecord, AccountTokens

DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
REFRESH_SKEW_SECONDS = 120
DEFAULT_CALLBACK_HOST = "127.0.0.1"
DEFAULT_CALLBACK_PORT = 1455
CODEX_OAUTH_SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"


class OAuthError(RuntimeError):
    pass


@dataclass
class LoginResult:
    account: AccountRecord
    base_url: str = DEFAULT_CODEX_BASE_URL


class BrowserCallbackServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler_class)
        self.expected_state = ""
        self.authorization_code: str | None = None
        self.callback_error: str | None = None
        self.callback_event = threading.Event()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = _b64url_encode(secrets.token_bytes(32))
    challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "PunkRecords/0.1"

    @property
    def callback_server(self) -> BrowserCallbackServer:
        return cast(BrowserCallbackServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return

        query = urllib.parse.parse_qs(parsed.query)
        code = str((query.get("code") or [""])[0]).strip()
        state = str((query.get("state") or [""])[0]).strip()

        if not code:
            self.callback_server.callback_error = "Missing authorization code in callback"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code")
            self.callback_server.callback_event.set()
            return

        if state != self.callback_server.expected_state:
            self.callback_server.callback_error = "OAuth state mismatch in callback"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch")
            self.callback_server.callback_event.set()
            return

        self.callback_server.authorization_code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>PunkRecords login complete</h1><p>You can return to the CLI.</p></body></html>")
        self.callback_server.callback_event.set()


DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "punkrecords/0.1.0",
}


def _json_post(url: str, payload: dict[str, object], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**DEFAULT_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise OAuthError(f"HTTP {exc.code} for {url}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Request failed for {url}: {exc}") from exc


def _form_post(url: str, payload: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode(),
        headers={**DEFAULT_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise OAuthError(f"HTTP {exc.code} for {url}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Request failed for {url}: {exc}") from exc


def decode_access_token_claims(access_token: str) -> dict[str, Any]:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return {}
        return json.loads(_b64url_decode(parts[1]).decode())
    except Exception:
        return {}


def _extract_profile(tokens: dict[str, Any]) -> tuple[str, str]:
    claims = decode_access_token_claims(str(tokens.get("access_token") or ""))
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    profile_claim = claims.get("https://api.openai.com/profile") or {}
    account_id = str(tokens.get("account_id") or auth_claim.get("chatgpt_account_id") or "")
    email = str(profile_claim.get("email") or claims.get("email") or "")
    return account_id, email


def _build_account(tokens: dict[str, Any], *, label: str | None, source: str) -> AccountRecord:
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise OAuthError("Token exchange response missing access_token or refresh_token")

    account_id, email = _extract_profile({"access_token": access_token, "account_id": tokens.get("account_id")})
    if not account_id:
        raise OAuthError("Could not determine account_id from OAuth response")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return AccountRecord(
        id=str(uuid.uuid4()),
        account_id=account_id,
        email=email,
        label=label or email or account_id,
        created_at=now,
        last_refresh=now,
        last_used=now,
        source=source,
        tokens=AccountTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_id,
        ),
    )


def access_token_expiring(access_token: str, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    claims = decode_access_token_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return float(exp) <= (time.time() + max(0, int(skew_seconds)))


def refresh_tokens(tokens: AccountTokens, timeout_seconds: float = 20.0) -> AccountTokens:
    payload = _form_post(
        CODEX_OAUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": CODEX_OAUTH_CLIENT_ID,
        },
        timeout=max(5.0, timeout_seconds),
    )
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or tokens.refresh_token).strip()
    if not access_token:
        raise OAuthError("Token refresh response did not include access_token")
    account_id, _ = _extract_profile({"access_token": access_token, "account_id": tokens.account_id})
    return AccountTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id or tokens.account_id,
    )


def maybe_refresh_account(account: AccountRecord) -> AccountRecord:
    if not access_token_expiring(account.tokens.access_token):
        return account
    refreshed = refresh_tokens(account.tokens)
    account.tokens = refreshed
    account.account_id = refreshed.account_id or account.account_id
    account.last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return account


def login_via_browser_flow(*, label: str | None = None) -> LoginResult:
    issuer = os.getenv("HERMES_CODEX_OAUTH_ISSUER", DEFAULT_ISSUER).strip().rstrip("/")
    token_url = os.getenv("HERMES_CODEX_OAUTH_TOKEN_URL", CODEX_OAUTH_TOKEN_URL).strip() or CODEX_OAUTH_TOKEN_URL
    client_id = os.getenv("HERMES_CODEX_OAUTH_CLIENT_ID", CODEX_OAUTH_CLIENT_ID).strip() or CODEX_OAUTH_CLIENT_ID
    callback_host = os.getenv("HERMES_CODEX_OAUTH_CALLBACK_HOST", DEFAULT_CALLBACK_HOST).strip() or DEFAULT_CALLBACK_HOST
    callback_port = int(os.getenv("HERMES_CODEX_OAUTH_CALLBACK_PORT", str(DEFAULT_CALLBACK_PORT)).strip() or str(DEFAULT_CALLBACK_PORT))

    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _generate_pkce_pair()
    originator = secrets.token_urlsafe(12)
    callback_server = BrowserCallbackServer((callback_host, callback_port), OAuthCallbackHandler)
    callback_server.expected_state = state
    callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
    callback_thread.start()

    redirect_uri = f"http://localhost:{callback_port}/auth/callback"
    authorize_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": CODEX_OAUTH_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": originator,
    }
    workspace_id = os.getenv("HERMES_CODEX_ALLOWED_WORKSPACE_ID", "").strip()
    if workspace_id:
        authorize_params["allowed_workspace_id"] = workspace_id

    authorize_url = f"{issuer}/oauth/authorize?{urllib.parse.urlencode(authorize_params)}"
    print("OpenAI Codex sign-in")
    print()
    print(f"Authorization URL: {authorize_url}")
    print()
    print(f"Waiting for browser callback on {redirect_uri} ... Press Ctrl+C to cancel.")
    print()

    try:
        opened = webbrowser.open(authorize_url)
        if opened:
            print("Opened browser automatically.")
        else:
            print("Could not open browser automatically. Open the URL manually.")

        completed = callback_server.callback_event.wait(timeout=15 * 60)
    except KeyboardInterrupt as exc:
        raise OAuthError("Login cancelled") from exc
    finally:
        callback_server.shutdown()
        callback_server.server_close()

    if not completed:
        raise OAuthError("Login timed out after 15 minutes")
    if callback_server.callback_error:
        raise OAuthError(callback_server.callback_error)
    authorization_code = str(callback_server.authorization_code or "").strip()
    if not authorization_code:
        raise OAuthError("Browser callback did not provide an authorization code")

    tokens = _form_post(
        token_url,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        timeout=15.0,
    )
    return LoginResult(account=_build_account(tokens, label=label, source="browser-flow"))


def login_via_device_flow(*, label: str | None = None, headless: bool = False) -> LoginResult:
    issuer = os.getenv("HERMES_CODEX_OAUTH_ISSUER", DEFAULT_ISSUER).strip().rstrip("/")
    token_url = os.getenv("HERMES_CODEX_OAUTH_TOKEN_URL", CODEX_OAUTH_TOKEN_URL).strip() or CODEX_OAUTH_TOKEN_URL
    client_id = os.getenv("HERMES_CODEX_OAUTH_CLIENT_ID", CODEX_OAUTH_CLIENT_ID).strip() or CODEX_OAUTH_CLIENT_ID

    device_data = _json_post(
        f"{issuer}/api/accounts/deviceauth/usercode",
        {"client_id": client_id},
        timeout=15.0,
    )
    user_code = str(device_data.get("user_code") or "").strip()
    device_auth_id = str(device_data.get("device_auth_id") or "").strip()
    poll_interval = max(3, int(device_data.get("interval") or 5))
    if not user_code or not device_auth_id:
        raise OAuthError("Device code response missing user_code or device_auth_id")

    verification_url = f"{issuer}/codex/device"
    print("OpenAI Codex sign-in")
    print()
    print(f"Verification URL: {verification_url}")
    print(f"User code: {user_code}")
    print()

    print("Waiting for OAuth completion... Press Ctrl+C to cancel.")
    max_wait = 15 * 60
    start = time.monotonic()
    code_response: dict[str, Any] | None = None
    try:
        while time.monotonic() - start < max_wait:
            time.sleep(poll_interval)
            try:
                code_response = _json_post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    {"device_auth_id": device_auth_id, "user_code": user_code},
                    timeout=15.0,
                )
                break
            except OAuthError as exc:
                message = str(exc)
                if "HTTP 403" in message or "HTTP 404" in message:
                    continue
                raise
    except KeyboardInterrupt as exc:
        raise OAuthError("Login cancelled") from exc

    if code_response is None:
        raise OAuthError("Login timed out after 15 minutes")

    authorization_code = str(code_response.get("authorization_code") or "").strip()
    code_verifier = str(code_response.get("code_verifier") or "").strip()
    redirect_uri = f"{issuer}/deviceauth/callback"
    if not authorization_code or not code_verifier:
        raise OAuthError("Authorization code exchange payload was incomplete")

    tokens = _form_post(
        token_url,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        timeout=15.0,
    )
    return LoginResult(account=_build_account(tokens, label=label, source="device-flow"))
