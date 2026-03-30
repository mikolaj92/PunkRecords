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
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, cast

from punkrecords.models import AccountRecord, AccountTokens, AccountUsage, UsageWindow
from punkrecords.providers.contracts import BrowserLoginChallenge, DeviceLoginChallenge, LocalRouteSpec, LoginResult, OAuthError, ProviderCapabilityProfile, ProviderDescriptor, ProviderRoutingDecision, ProxyRequestSpec, StreamUsageTracker, UsageSummary

DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
REFRESH_SKEW_SECONDS = 120
DEFAULT_CALLBACK_HOST = "localhost"
DEFAULT_CALLBACK_PORT = 1455
CODEX_OAUTH_SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "punkrecords/0.1.0",
}
ROUTE_MAP = {
    "/v1/responses": "/responses",
    "/v1/chat/completions": "/responses",
    "/v1/embeddings": "/embeddings",
}

_PENDING_BROWSER_LOGINS: dict[str, "BrowserCallbackServer"] = {}


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
    token_payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
    }
    return AccountRecord(
        id=str(uuid.uuid4()),
        account_id=account_id,
        email=email,
        label=label or email or account_id,
        provider="openai-codex",
        created_at=now,
        last_refresh=now,
        last_used=now,
        auth_mode="chatgpt",
        source=source,
        provider_state={"tokens": token_payload, "auth_mode": "chatgpt"},
        tokens=None,
    )


def _provider_tokens(account: AccountRecord) -> AccountTokens:
    provider_tokens = account.provider_state.get("tokens") if isinstance(account.provider_state, dict) else None
    if isinstance(provider_tokens, dict):
        access_token = str(provider_tokens.get("access_token") or "")
        refresh_token = str(provider_tokens.get("refresh_token") or "")
        account_id = str(provider_tokens.get("account_id") or account.account_id or (account.tokens.account_id if account.tokens is not None else ""))
        return AccountTokens(access_token=access_token, refresh_token=refresh_token, account_id=account_id)
    return account.tokens or AccountTokens()


def usage_url() -> str:
    override = os.getenv("PUNKRECORDS_OPENAI_CODEX_USAGE_URL", "").strip()
    if override:
        return override

    base_url = os.getenv("PUNKRECORDS_OPENAI_CODEX_BASE_URL", DEFAULT_CODEX_BASE_URL).strip() or DEFAULT_CODEX_BASE_URL
    if base_url.endswith("/codex"):
        return base_url[: -len("/codex")] + "/wham/usage"
    return base_url.rstrip("/") + "/wham/usage"


def _coerce_window(payload: dict[str, Any] | None) -> UsageWindow:
    if not isinstance(payload, dict):
        return UsageWindow()
    used_percent = payload.get("used_percent")
    return UsageWindow(
        used_percent=float(used_percent) if isinstance(used_percent, (int, float)) else None,
        limit_window_seconds=int(payload["limit_window_seconds"]) if isinstance(payload.get("limit_window_seconds"), (int, float)) else None,
        reset_after_seconds=int(payload["reset_after_seconds"]) if isinstance(payload.get("reset_after_seconds"), (int, float)) else None,
        reset_at=int(payload["reset_at"]) if isinstance(payload.get("reset_at"), (int, float)) else None,
    )


def access_token_expiring(access_token: str, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    claims = decode_access_token_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return float(exp) <= (time.time() + max(0, int(skew_seconds)))


def proxy_upstream_base() -> str:
    override = os.getenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_CODEX_BASE_URL.rstrip("/")


def proxy_upstream_url(local_path: str) -> str:
    legacy_override = os.getenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL", "").strip()
    if local_path == "/v1/responses" and legacy_override:
        return legacy_override

    path = ROUTE_MAP.get(local_path)
    if not path:
        raise KeyError(local_path)

    specific_key = f"PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_{local_path.strip('/').replace('/', '_').upper()}_URL"
    override = os.getenv(specific_key, "").strip()
    if override:
        return override
    return proxy_upstream_base() + path


def proxy_extract_usage(payload: dict[str, Any], local_path: str) -> UsageSummary:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    if local_path == "/v1/embeddings":
        return {
            "input_tokens": int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
            "output_tokens": 0,
            "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
        }

    if isinstance(usage.get("input_tokens"), int):
        return {
            "input_tokens": usage["input_tokens"],
            "output_tokens": int(usage["output_tokens"]) if isinstance(usage.get("output_tokens"), int) else None,
            "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
        }

    return {
        "input_tokens": int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
        "output_tokens": int(usage["completion_tokens"]) if isinstance(usage.get("completion_tokens"), int) else None,
        "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
    }


def _format_reset(window: dict[str, Any]) -> str:
    reset_after_seconds = window.get("reset_after_seconds")
    reset_at = window.get("reset_at")
    parts: list[str] = []
    if isinstance(reset_after_seconds, (int, float)):
        seconds = int(reset_after_seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
    if parts:
        return "reset in " + " ".join(parts)
    if isinstance(reset_at, (int, float)):
        return f"reset at {int(reset_at)}"
    return "reset unknown"


def _window_summary(usages: list[AccountUsage], key: str) -> dict[str, int | float | None]:
    reported = 0
    used_percent_total = 0.0
    reset_after_seconds_min: int | None = None
    reset_at_min: int | None = None
    for usage in usages:
        if usage.error:
            continue
        window = getattr(usage, key).to_dict()
        used_percent = window.get("used_percent")
        if not isinstance(used_percent, (int, float)):
            continue
        reported += 1
        used_percent_total += float(used_percent)
        reset_after_seconds = window.get("reset_after_seconds")
        if isinstance(reset_after_seconds, (int, float)):
            value = int(reset_after_seconds)
            reset_after_seconds_min = value if reset_after_seconds_min is None else min(reset_after_seconds_min, value)
        reset_at = window.get("reset_at")
        if isinstance(reset_at, (int, float)):
            value = int(reset_at)
            reset_at_min = value if reset_at_min is None else min(reset_at_min, value)
    return {
        "used_percent_total": round(used_percent_total, 2),
        "capacity_percent_total": reported * 100.0,
        "reported_accounts": reported,
        "reset_after_seconds": reset_after_seconds_min,
        "reset_after_seconds_min": reset_after_seconds_min,
        "reset_at": reset_at_min,
    }


def _build_codex_usage_rows(usages: list[AccountUsage]) -> list[list[str]]:
    rows: list[list[str]] = []
    for usage in usages:
        if usage.error:
            rows.append([usage.display_name, usage.provider or "unknown", "unknown", "error", str(usage.error), "error", str(usage.error)])
            continue
        rows.append(
            [
                usage.display_name,
                usage.provider or "unknown",
                usage.plan_type or "unknown",
                f"{usage.primary_window.used_percent}%" if usage.primary_window.used_percent is not None else "unknown",
                _format_reset(usage.primary_window.to_dict()),
                f"{usage.secondary_window.used_percent}%" if usage.secondary_window.used_percent is not None else "unknown",
                _format_reset(usage.secondary_window.to_dict()),
            ]
        )
    return rows


def build_codex_usage_report(usages: list[AccountUsage]) -> dict[str, Any]:
    five_hour = _window_summary(usages, "primary_window")
    weekly = _window_summary(usages, "secondary_window")
    failed_accounts = [usage.display_name for usage in usages if usage.error]
    columns = ["Credential", "Provider", "Plan", "5h", "5h reset", "Week", "Week reset"]
    rows = _build_codex_usage_rows(usages)
    summary_lines = [
        f"Reported 5h:   {five_hour['used_percent_total']}% / {five_hour['capacity_percent_total']}% across {five_hour['reported_accounts']} credential(s), {_format_reset(five_hour)}",
        f"Reported week: {weekly['used_percent_total']}% / {weekly['capacity_percent_total']}% across {weekly['reported_accounts']} credential(s), {_format_reset(weekly)}",
    ]
    if failed_accounts:
        summary_lines.append(f"Usage errors:  {', '.join(failed_accounts)}")
    return {
        "provider_id": "openai-codex",
        "title": "Usage",
        "subtitle": "Credential quota snapshots from the upstream usage endpoint.",
        "cards": [
            {
                "title": "5h window",
                "detail": f"{five_hour['reported_accounts']} credential(s) reported.",
                "value": f"{five_hour['used_percent_total']}% / {five_hour['capacity_percent_total']}%",
                "meta": _format_reset(five_hour),
            },
            {
                "title": "Weekly window",
                "detail": f"{weekly['reported_accounts']} credential(s) reported.",
                "value": f"{weekly['used_percent_total']}% / {weekly['capacity_percent_total']}%",
                "meta": _format_reset(weekly),
            },
        ],
        "columns": columns,
        "rows": rows,
        "table_lines": build_codex_usage_table(usages, columns=columns, rows=rows),
        "summary_lines": summary_lines,
        "summary": {
            "5h": five_hour,
            "weekly": weekly,
            "failed_accounts": failed_accounts,
        },
    }


def _table_separator(widths: list[int]) -> str:
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def _table_row(values: list[str], widths: list[int]) -> str:
    padded = [f" {value.ljust(width)} " for value, width in zip(values, widths, strict=True)]
    return "|" + "|".join(padded) + "|"


def build_codex_usage_table(usages: list[AccountUsage], *, columns: list[str] | None = None, rows: list[list[str]] | None = None) -> list[str]:
    headers = columns or ["Credential", "Provider", "Plan", "5h", "5h reset", "Week", "Week reset"]
    if rows is None:
        rows = _build_codex_usage_rows(usages)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = [_table_separator(widths), _table_row(headers, widths), _table_separator(widths)]
    lines.extend(_table_row(row, widths) for row in rows)
    lines.append(_table_separator(widths))
    return lines


def describe_codex_routes(base_url: str) -> list[tuple[str, str]]:
    return [
        ("Responses route", f"{base_url}/v1/responses"),
        ("Chat completions route", f"{base_url}/v1/chat/completions"),
        ("Embeddings route", f"{base_url}/v1/embeddings"),
        ("Models route", f"{base_url}/v1/models"),
    ]


class OpenAICodexStreamUsageTracker:
    def __init__(self, local_path: str) -> None:
        self.local_path = local_path
        self.buffer = ""
        self.usage: UsageSummary = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk.decode(errors="replace")
        while "\n\n" in self.buffer:
            raw_event, self.buffer = self.buffer.split("\n\n", 1)
            self._parse_event(raw_event)

    def _parse_event(self, raw_event: str) -> None:
        lines = [line for line in raw_event.splitlines() if line]
        if not lines:
            return
        event_name = None
        data_parts: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_parts.append(line.removeprefix("data:").strip())
        if not data_parts:
            return
        data_payload = "\n".join(data_parts)
        if data_payload == "[DONE]":
            return
        try:
            payload = json.loads(data_payload)
        except json.JSONDecodeError:
            return

        if event_name == "response.completed" and isinstance(payload, dict):
            response = payload.get("response")
            if isinstance(response, dict):
                self.usage = proxy_extract_usage(response, self.local_path)


def codex_models_payload() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "gpt-5.4", "object": "model", "created": 1743091200, "owned_by": "openai"},
            {"id": "gpt-5.4-mini", "object": "model", "created": 1743091200, "owned_by": "openai"},
            {"id": "text-embedding-3-small", "object": "model", "created": 1743091200, "owned_by": "openai"},
        ],
    }


def responses_api_to_chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output", [])
    message_content = ""
    tool_calls = None
    for item in output if isinstance(output, list) else []:
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    message_content += part.get("text", "")
        elif item_type == "function_call":
            if tool_calls is None:
                tool_calls = []
            tool_calls.append({
                "id": item.get("call_id", item.get("id", "")),
                "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")},
            })
    usage = payload.get("usage", {})
    return {
        "id": payload.get("id", "chatcmpl-responses-proxy"),
        "object": "chat.completion",
        "created": payload.get("created_at", 0),
        "model": payload.get("model", ""),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": message_content or None, "tool_calls": tool_calls},
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        },
    }


def chat_completions_to_responses_api(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages", [])
    instructions = None
    input_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if instructions is None:
                instructions = content if isinstance(content, str) else ""
            continue

        if role in ("user", "assistant", "developer"):
            input_messages.append({
                "role": role,
                "content": content if isinstance(content, str) else "",
            })

    result: dict[str, Any] = {
        "model": payload.get("model", ""),
        "input": input_messages,
        "stream": payload.get("stream", False),
        "store": False,
    }

    if instructions:
        result["instructions"] = instructions

    if payload.get("tools"):
        result["tools"] = [
            {
                "type": "function",
                "name": t.get("function", {}).get("name", ""),
                "description": t.get("function", {}).get("description", ""),
                "parameters": t.get("function", {}).get("parameters"),
            }
            for t in payload.get("tools", [])
        ]
        if payload.get("tool_choice"):
            result["tool_choice"] = payload["tool_choice"]

    return result


def classify_codex_status(status_code: int, body: bytes) -> tuple[bool, int]:
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True, 60

    if status_code == 402:
        try:
            payload = json.loads(body.decode() or "{}")
        except Exception:
            return False, 0
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if code in {"deactivated_workspace", "usage_limit_reached", "rate_limited"}:
            return True, 300
    return False, 0


def _body_error_code(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode() or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return str(error.get("code"))
    detail = payload.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return str(detail.get("code"))
    return None


@dataclass
class OpenAICodexProvider:
    provider_id: str = "openai-codex"
    label: str = "OpenAI Codex"

    def login_via_browser_flow(self, *, label: str | None = None) -> LoginResult:
        issuer = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_ISSUER", DEFAULT_ISSUER).strip().rstrip("/")
        token_url = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_TOKEN_URL", CODEX_OAUTH_TOKEN_URL).strip() or CODEX_OAUTH_TOKEN_URL
        client_id = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CLIENT_ID", CODEX_OAUTH_CLIENT_ID).strip() or CODEX_OAUTH_CLIENT_ID
        callback_host = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CALLBACK_HOST", DEFAULT_CALLBACK_HOST).strip() or DEFAULT_CALLBACK_HOST
        callback_port = int(os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CALLBACK_PORT", str(DEFAULT_CALLBACK_PORT)).strip() or str(DEFAULT_CALLBACK_PORT))

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
        workspace_id = os.getenv("PUNKRECORDS_OPENAI_CODEX_ALLOWED_WORKSPACE_ID", "").strip()
        if workspace_id:
            authorize_params["allowed_workspace_id"] = workspace_id

        authorize_url = f"{issuer}/oauth/authorize?{urllib.parse.urlencode(authorize_params)}"
        print("OpenAI Codex sign-in")
        print()
        print("Copy and open the URL below in your browser to sign in:")
        print()
        print(f"  {authorize_url}")
        print()
        print(f"Waiting for browser callback on {redirect_uri} ... Press Ctrl+C to cancel.")
        print()

        try:

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
        return LoginResult(account=_build_account(tokens, label=label, source="browser-flow"), base_url=DEFAULT_CODEX_BASE_URL)

    def start_device_login(self, *, label: str | None = None) -> DeviceLoginChallenge:
        issuer = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_ISSUER", DEFAULT_ISSUER).strip().rstrip("/")
        token_url = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_TOKEN_URL", CODEX_OAUTH_TOKEN_URL).strip() or CODEX_OAUTH_TOKEN_URL
        client_id = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CLIENT_ID", CODEX_OAUTH_CLIENT_ID).strip() or CODEX_OAUTH_CLIENT_ID

        device_data = _json_post(
            f"{issuer}/api/accounts/deviceauth/usercode",
            {"client_id": client_id},
            timeout=15.0,
        )
        user_code = str(device_data.get("user_code") or "").strip()
        device_auth_id = str(device_data.get("device_auth_id") or "").strip()
        poll_interval = max(1, int(device_data.get("interval") or 5))
        if not user_code or not device_auth_id:
            raise OAuthError("Device code response missing user_code or device_auth_id")

        return DeviceLoginChallenge(
            provider_id=self.provider_id,
            device_auth_id=device_auth_id,
            user_code=user_code,
            verification_url=f"{issuer}/codex/device",
            poll_interval=poll_interval,
            issuer=issuer,
            token_url=token_url,
            client_id=client_id,
            label=label,
        )

    def poll_device_login(self, challenge: DeviceLoginChallenge) -> LoginResult | None:
        try:
            code_response = _json_post(
                f"{challenge.issuer}/api/accounts/deviceauth/token",
                {"device_auth_id": challenge.device_auth_id, "user_code": challenge.user_code},
                timeout=15.0,
            )
        except OAuthError as exc:
            message = str(exc)
            if "HTTP 403" in message or "HTTP 404" in message:
                return None
            raise

        authorization_code = str(code_response.get("authorization_code") or "").strip()
        code_verifier = str(code_response.get("code_verifier") or "").strip()
        redirect_uri = f"{challenge.issuer}/deviceauth/callback"
        if not authorization_code or not code_verifier:
            raise OAuthError("Authorization code exchange payload was incomplete")

        tokens = _form_post(
            challenge.token_url,
            {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": redirect_uri,
                "client_id": challenge.client_id,
                "code_verifier": code_verifier,
            },
            timeout=15.0,
        )
        return LoginResult(account=_build_account(tokens, label=challenge.label, source="device-flow"), base_url=DEFAULT_CODEX_BASE_URL)

    def start_browser_login(self, *, label: str | None = None, redirect_uri: str | None = None) -> BrowserLoginChallenge:
        issuer = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_ISSUER", DEFAULT_ISSUER).strip().rstrip("/")
        token_url = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_TOKEN_URL", CODEX_OAUTH_TOKEN_URL).strip() or CODEX_OAUTH_TOKEN_URL
        client_id = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CLIENT_ID", CODEX_OAUTH_CLIENT_ID).strip() or CODEX_OAUTH_CLIENT_ID
        callback_host = os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CALLBACK_HOST", DEFAULT_CALLBACK_HOST).strip() or DEFAULT_CALLBACK_HOST
        callback_port = int(os.getenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_CALLBACK_PORT", str(DEFAULT_CALLBACK_PORT)).strip() or str(DEFAULT_CALLBACK_PORT))

        state = secrets.token_urlsafe(24)
        code_verifier, code_challenge = _generate_pkce_pair()
        originator = secrets.token_urlsafe(12)
        
        callback_server = BrowserCallbackServer((callback_host, callback_port), OAuthCallbackHandler)
        callback_server.expected_state = state
        callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
        callback_thread.start()
        
        callback_uri = f"http://localhost:{callback_port}/auth/callback"
        
        authorize_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": callback_uri,
            "scope": CODEX_OAUTH_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": originator,
        }
        workspace_id = os.getenv("PUNKRECORDS_OPENAI_CODEX_ALLOWED_WORKSPACE_ID", "").strip()
        if workspace_id:
            authorize_params["allowed_workspace_id"] = workspace_id

        authorize_url = f"{issuer}/oauth/authorize?{urllib.parse.urlencode(authorize_params)}"

        _PENDING_BROWSER_LOGINS[state] = callback_server

        return BrowserLoginChallenge(
            provider_id=self.provider_id,
            authorize_url=authorize_url,
            redirect_uri=callback_uri,
            code_verifier=code_verifier,
            state=state,
            issuer=issuer,
            token_url=token_url,
            client_id=client_id,
            label=label,
        )

    def wait_browser_login_callback(self, state: str, timeout: float = 300.0) -> str:
        callback_server = _PENDING_BROWSER_LOGINS.get(state)
        if not callback_server:
            raise OAuthError("No pending browser login for this state")
        try:
            completed = callback_server.callback_event.wait(timeout=timeout)
        finally:
            callback_server.shutdown()
            callback_server.server_close()
            _PENDING_BROWSER_LOGINS.pop(state, None)

        if not completed:
            raise OAuthError("Browser login timed out")
        if callback_server.callback_error:
            raise OAuthError(callback_server.callback_error)
        authorization_code = str(callback_server.authorization_code or "").strip()
        if not authorization_code:
            raise OAuthError("Browser callback did not provide an authorization code")
        return authorization_code

    def complete_browser_login(self, challenge: BrowserLoginChallenge, authorization_code: str) -> LoginResult:
        tokens = _form_post(
            challenge.token_url,
            {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": challenge.redirect_uri,
                "client_id": challenge.client_id,
                "code_verifier": challenge.code_verifier,
            },
            timeout=15.0,
        )
        return LoginResult(account=_build_account(tokens, label=challenge.label, source="browser-flow"), base_url=DEFAULT_CODEX_BASE_URL)

    def login_via_device_flow(self, *, label: str | None = None, headless: bool = False) -> LoginResult:
        del headless
        challenge = self.start_device_login(label=label)
        print("OpenAI Codex sign-in")
        print()
        print(f"Verification URL: {challenge.verification_url}")
        print(f"User code: {challenge.user_code}")
        print()

        print("Waiting for OAuth completion... Press Ctrl+C to cancel.")
        max_wait = 15 * 60
        start = time.monotonic()
        result: LoginResult | None = None
        try:
            while time.monotonic() - start < max_wait:
                time.sleep(challenge.poll_interval)
                result = self.poll_device_login(challenge)
                if result is not None:
                    break
        except KeyboardInterrupt as exc:
            raise OAuthError("Login cancelled") from exc

        if result is None:
            raise OAuthError("Login timed out after 15 minutes")
        return result

    def refresh_tokens(self, tokens: AccountTokens, timeout_seconds: float = 20.0) -> AccountTokens:
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

    def maybe_refresh_account(self, account: AccountRecord) -> AccountRecord:
        current_tokens = _provider_tokens(account)
        if not access_token_expiring(current_tokens.access_token):
            return account
        refreshed = self.refresh_tokens(current_tokens)
        account.provider_state = {**account.provider_state, "tokens": refreshed.to_dict(), "auth_mode": account.auth_mode}
        account.account_id = refreshed.account_id or account.account_id
        account.last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return account

    def fetch_account_usage(self, account: AccountRecord, timeout: float = 15.0) -> tuple[AccountRecord, AccountUsage]:
        refreshed_account = account
        try:
            refreshed_account = self.maybe_refresh_account(account)
            refreshed_tokens = _provider_tokens(refreshed_account)
            request = urllib.request.Request(
                usage_url(),
                headers={
                    **DEFAULT_HEADERS,
                    "Authorization": f"Bearer {refreshed_tokens.access_token}",
                    "ChatGPT-Account-Id": refreshed_tokens.account_id or refreshed_account.account_id,
                    "OpenAI-Beta": "responses=v1",
                    "OpenAI-Originator": "codex",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            return refreshed_account, AccountUsage(
                account_id=refreshed_account.account_id,
                label=refreshed_account.label,
                provider=refreshed_account.provider,
                plan_type=None,
                error=f"HTTP {exc.code}: {body or exc.reason}",
            )
        except (urllib.error.URLError, OAuthError, json.JSONDecodeError) as exc:
            return refreshed_account, AccountUsage(
                account_id=refreshed_account.account_id,
                label=refreshed_account.label,
                provider=refreshed_account.provider,
                plan_type=None,
                error=str(exc),
            )

        rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
        usage = AccountUsage(
            account_id=refreshed_account.account_id,
            label=refreshed_account.label,
            provider=refreshed_account.provider,
            plan_type=str(payload.get("plan_type")) if isinstance(payload, dict) and payload.get("plan_type") is not None else None,
            primary_window=_coerce_window(rate_limit.get("primary_window") if isinstance(rate_limit, dict) else None),
            secondary_window=_coerce_window(rate_limit.get("secondary_window") if isinstance(rate_limit, dict) else None),
        )
        return refreshed_account, usage

    def usage_url(self) -> str:
        return usage_url()

    def build_usage_report(self, usages: list[AccountUsage]) -> dict[str, Any]:
        return build_codex_usage_report(usages)

    def format_usage_table(self, usages: list[AccountUsage]) -> list[str]:
        return build_codex_usage_table(usages)

    def local_paths(self) -> tuple[str, ...]:
        return tuple(ROUTE_MAP.keys())

    def local_routes(self) -> tuple[LocalRouteSpec, ...]:
        return tuple(LocalRouteSpec(path=path, method="POST") for path in ROUTE_MAP.keys())

    def parse_local_request(self, *, local_path: str, method: str, raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        del headers
        if method.upper() != "POST" or local_path not in ROUTE_MAP:
            raise ValueError("unsupported route")
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Request body is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def is_streaming_request(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("stream"))

    def matches_request(self, local_path: str, payload: dict[str, Any]) -> bool:
        del payload
        return local_path in ROUTE_MAP

    def proxy_upstream_url(self, local_path: str) -> str:
        return proxy_upstream_url(local_path)

    def build_proxy_request(self, account: AccountRecord, *, local_path: str, payload: dict[str, Any], idempotency_key: str) -> ProxyRequestSpec:
        upstream_payload = chat_completions_to_responses_api(payload) if local_path == "/v1/chat/completions" else payload
        data = json.dumps(upstream_payload).encode()
        stream = self.is_streaming_request(payload)
        return ProxyRequestSpec(
            url=self.proxy_upstream_url(local_path),
            data=data,
            headers=self.proxy_headers(account, stream=stream, idempotency_key=idempotency_key),
            method="POST",
        )

    def proxy_headers(self, account: AccountRecord, *, stream: bool, idempotency_key: str) -> dict[str, str]:
        tokens = _provider_tokens(account)
        return {
            **DEFAULT_HEADERS,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tokens.access_token}",
            "ChatGPT-Account-Id": tokens.account_id or account.account_id,
            "OpenAI-Beta": "responses=v1",
            "OpenAI-Originator": "codex",
            "Idempotency-Key": idempotency_key,
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def proxy_extract_usage(self, payload: dict[str, Any], local_path: str) -> UsageSummary:
        return proxy_extract_usage(payload, local_path)

    def proxy_extract_usage_from_body(self, body: bytes, local_path: str) -> UsageSummary:
        return proxy_extract_usage(json.loads(body.decode()), local_path)

    def create_stream_usage_tracker(self, local_path: str) -> StreamUsageTracker:
        return OpenAICodexStreamUsageTracker(local_path)

    def list_models(self) -> dict[str, Any]:
        return codex_models_payload()

    def classify_proxy_failure(self, status_code: int, body: bytes) -> tuple[bool, int]:
        return classify_codex_status(status_code, body)

    def classify_routing_failure(self, status_code: int, body: bytes) -> ProviderRoutingDecision:
        retryable, _ = self.classify_proxy_failure(status_code, body)
        if retryable:
            return ProviderRoutingDecision(True, "retryable_provider_failure")
        error_code = _body_error_code(body)
        if error_code in {"no_eligible_accounts", "all_accounts_failed", "upstream_connection_error", "account_refresh_failed"}:
            return ProviderRoutingDecision(True, error_code)
        return ProviderRoutingDecision(False, error_code or "fatal_or_invalid_request")

    def capability_profile(self) -> ProviderCapabilityProfile:
        return ProviderCapabilityProfile(
            model_ids=("gpt-5.4", "gpt-5.4-mini", "text-embedding-3-small"),
            supports_streaming=True,
            supports_tools=True,
            supports_embeddings=True,
        )

    def describe_local_routes(self, *, base_url: str) -> list[tuple[str, str]]:
        return describe_codex_routes(base_url)

_OPENAI_CODEX_PROVIDER = OpenAICodexProvider()
PROVIDER = ProviderDescriptor(
    provider_id=_OPENAI_CODEX_PROVIDER.provider_id,
    label=_OPENAI_CODEX_PROVIDER.label,
    auth=_OPENAI_CODEX_PROVIDER,
    usage=_OPENAI_CODEX_PROVIDER,
    proxy=_OPENAI_CODEX_PROVIDER,
)
