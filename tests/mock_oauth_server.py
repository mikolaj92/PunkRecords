from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, cast
from urllib.parse import parse_qs, urlencode, urlparse


def _jwt_segment(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _access_token(account_id: str, email: str) -> str:
    payload = {
        "exp": 4774686340,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "https://api.openai.com/profile": {"email": email},
    }
    return f"header.{_jwt_segment(payload)}.sig"


class OAuthState:
    def __init__(self) -> None:
        self.device_counter = 0
        self.browser_counter = 100
        self.device_map: dict[str, dict[str, str]] = {}
        self.code_map: dict[str, dict[str, str]] = {}

    def new_device(self) -> dict[str, str | int]:
        self.device_counter += 1
        index = self.device_counter
        auth_id = f"device-{index}"
        auth_code = f"auth-code-{index}"
        verifier = f"verifier-{index}"
        account_id = f"acct-{index}"
        email = f"user{index}@example.com"
        self.device_map[auth_id] = {
            "authorization_code": auth_code,
            "code_verifier": verifier,
            "account_id": account_id,
            "email": email,
        }
        self.code_map[auth_code] = self.device_map[auth_id]
        return {
            "user_code": f"CODE-{index:04d}",
            "device_auth_id": auth_id,
            "interval": 0,
        }

    def new_browser_code(self) -> dict[str, str]:
        self.browser_counter += 1
        index = self.browser_counter
        auth_code = f"browser-code-{index}"
        account_id = f"acct-{index}"
        email = f"browser{index}@example.com"
        data = {
            "authorization_code": auth_code,
            "code_verifier": f"browser-verifier-{index}",
            "account_id": account_id,
            "email": email,
        }
        self.code_map[auth_code] = data
        return data


class MockOAuthServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler_class)
        self.state = OAuthState()


class OAuthHandler(BaseHTTPRequestHandler):
    server_version = "MockOAuth/0.1"

    @property
    def state(self) -> OAuthState:
        return cast(MockOAuthServer, self.server).state

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _form_body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode() if length > 0 else ""
        parsed = parse_qs(raw)
        return {key: values[0] for key, values in parsed.items() if values}

    def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/accounts/deviceauth/usercode":
            self._json_body()
            self._send_json(200, self.state.new_device())
            return

        if self.path == "/api/accounts/deviceauth/token":
            payload = self._json_body()
            device_auth_id = str(payload.get("device_auth_id") or "")
            data = self.state.device_map.get(device_auth_id)
            if not data:
                self._send_json(404, {"error": "unknown_device"})
                return
            self._send_json(
                200,
                {
                    "authorization_code": data["authorization_code"],
                    "code_verifier": data["code_verifier"],
                },
            )
            return

        if self.path == "/oauth/token":
            payload = self._form_body()
            grant_type = payload.get("grant_type")
            if grant_type == "authorization_code":
                data = self.state.code_map.get(str(payload.get("code") or ""))
                if not data:
                    self._send_json(400, {"error": "invalid_code"})
                    return
                self._send_json(
                    200,
                    {
                        "access_token": _access_token(data["account_id"], data["email"]),
                        "refresh_token": f"refresh-{data['account_id']}",
                        "account_id": data["account_id"],
                    },
                )
                return
            if grant_type == "refresh_token":
                refresh_token = str(payload.get("refresh_token") or "")
                account_id = refresh_token.removeprefix("refresh-") or "acct-refresh"
                email = f"{account_id}@example.com"
                self._send_json(
                    200,
                    {
                        "access_token": _access_token(account_id, email),
                        "refresh_token": refresh_token,
                        "account_id": account_id,
                    },
                )
                return

        self._send_json(404, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/oauth/authorize":
            query = parse_qs(parsed.query)
            redirect_uri = str((query.get("redirect_uri") or [""])[0]).strip()
            state = str((query.get("state") or [""])[0]).strip()
            if not redirect_uri:
                self._send_json(400, {"error": "missing_redirect_uri"})
                return

            data = self.state.new_browser_code()
            location = f"{redirect_uri}?{urlencode({'code': data['authorization_code'], 'state': state})}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return

        if parsed.path == "/codex/device":
            body = b"<html><body><h1>Mock device page</h1></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self._send_json(404, {"error": "not_found"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = MockOAuthServer(("localhost", args.port), OAuthHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
