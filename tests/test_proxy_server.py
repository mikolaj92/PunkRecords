from __future__ import annotations

import importlib
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

models_module = importlib.import_module("punkrecords.models")
oauth_module = importlib.import_module("punkrecords.oauth")
proxy_module = importlib.import_module("punkrecords.proxy")
providers_module = importlib.import_module("punkrecords.providers")
settings_store_module = importlib.import_module("punkrecords.settings_store")
stats_store_module = importlib.import_module("punkrecords.stats_store")
store_module = importlib.import_module("punkrecords.store")
mock_oauth_module = importlib.import_module("tests.mock_oauth_server")

AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
OAuthError = oauth_module.OAuthError
ProxyHandler = proxy_module.ProxyHandler
ProxyServer = proxy_module.ProxyServer
MockOAuthServer = mock_oauth_module.MockOAuthServer
OAuthHandler = mock_oauth_module.OAuthHandler
get_provider = providers_module.get_provider
LoginResult = providers_module.LoginResult


def _account(index: int, label: str, provider: str = "openai-codex") -> AccountRecord:
    return AccountRecord(
        id=f"local-{index}",
        account_id=f"acct-{index}",
        email=f"user{index}@example.com",
        label=label,
        provider=provider,
        created_at="2026-03-27T00:00:00Z",
        last_refresh="2026-03-27T00:00:00Z",
        last_used="2026-03-27T00:00:00Z",
        tokens=AccountTokens(
            access_token=f"token-{index}",
            refresh_token=f"refresh-{index}",
            account_id=f"acct-{index}",
        ),
    )


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("localhost", 0))
    _, port = sock.getsockname()
    sock.close()
    return int(port)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        account_id = self.headers.get("ChatGPT-Account-Id", "")
        if self.path != "/responses":
            self.send_response(404)
            self.end_headers()
            return

        if account_id == "acct-1":
            body = json.dumps({"detail": {"code": "deactivated_workspace"}}).encode()
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({"ok": True, "account_id": account_id}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EmbeddingsFailoverHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        account_id = self.headers.get("ChatGPT-Account-Id", "")
        if self.path != "/embeddings":
            self.send_response(404)
            self.end_headers()
            return

        if account_id == "acct-1":
            body = json.dumps({"detail": {"code": "deactivated_workspace"}}).encode()
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 6, "total_tokens": 6},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SuccessUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/embeddings":
            payload = {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 6, "total_tokens": 6},
            }
            body = json.dumps(payload).encode()
        else:
            body = json.dumps({"ok": True, "account_id": self.headers.get("ChatGPT-Account-Id", ""), "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StreamingUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = (
            'event: response.created\n'
            'data: {"type":"response.created"}\n\n'
            'event: response.output_text.delta\n'
            'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
            'event: response.completed\n'
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":4,"total_tokens":13}}}\n\n'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        probe = socket.socket()
        try:
            probe.settimeout(0.2)
            probe.connect(("localhost", server.server_port))
            probe.close()
            break
        except OSError:
            probe.close()
            time.sleep(0.05)
    return thread


def test_proxy_healthz(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["ok"] is True
        assert payload["accounts"] == 1
        assert payload["eligible_accounts"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_models_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/v1/models", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["object"] == "list"
        assert [item["id"] for item in payload["data"]] == ["gpt-5.4", "gpt-5.4-mini", "text-embedding-3-small"]
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_openapi_docs_and_dashboard(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    stats_store_module.record_request(
        {
            "endpoint": "/v1/responses",
            "status_code": 200,
            "latency_ms": 123,
            "account_id": "acct-1",
            "provider_id": "openai-codex",
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
        }
    )

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/openapi.json", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert response.status == 200
        assert payload["openapi"] == "3.1.0"
        assert "/v1/responses" in payload["paths"]

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/docs", timeout=5) as response:
            html = response.read().decode()
        assert "Swagger UI" in html

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/", timeout=5) as response:
            dashboard_html = response.read().decode()
        assert response.status == 200
        assert "<main id=\"page-content\">" in dashboard_html
        assert "hx-get=\"/_proxy/dashboard/overview\"" in dashboard_html
        assert "chart.js" in dashboard_html.lower()

        try:
            urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode())
            assert payload["error"]["code"] == "not_found"
        else:
            raise AssertionError("Expected legacy dashboard route to be removed")

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/overview", timeout=5) as response:
            overview_html = response.read().decode()
        assert response.status == 200
        assert "Overview" in overview_html
        assert "Total tokens" in overview_html
        assert "Input tokens" in overview_html
        assert "Output tokens" in overview_html
        assert "<html" not in overview_html.lower()

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/charts", timeout=5) as response:
            charts_html = response.read().decode()
        assert response.status == 200
        assert "Charts" in charts_html
        assert "data-chart-kind=" in charts_html
        assert "<html" not in charts_html.lower()

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/accounts", timeout=5) as response:
            accounts_html = response.read().decode()
        assert response.status == 200
        assert "Credentials" in accounts_html
        assert "Add account" in accounts_html
        assert "Start device login" in accounts_html
        assert "complete from any device" in accounts_html
        assert "acct-1" in accounts_html
        assert "<html" not in accounts_html.lower()

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/requests?limit=20", timeout=5) as response:
            requests_html = response.read().decode()
        assert response.status == 200
        assert "<th>Input</th>" in requests_html
        assert "<th>Output</th>" in requests_html
        assert "<th>Total</th>" in requests_html
        assert ">12<" in requests_html
        assert ">8<" in requests_html
        assert ">20<" in requests_html
        assert "<html" not in requests_html.lower()

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/settings", timeout=5) as response:
            settings_html = response.read().decode()
        assert "Save settings" in settings_html
        assert "<body" not in settings_html.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_accounts_device_flow_uses_existing_oauth_helpers(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()

    challenge = providers_module.DeviceLoginChallenge(
        provider_id="openai-codex",
        device_auth_id="device-auth-id",
        user_code="ABCD-EFGH",
        verification_url="https://chatgpt.com/device",
        poll_interval=5,
        issuer="https://auth.openai.com",
        token_url="https://auth.openai.com/oauth/token",
        client_id="client-id",
        label="Team account",
    )

    def fake_start_device_login(*, provider_id=None, label=None):
        assert provider_id == "openai-codex"
        assert label == "Team account"
        return challenge

    def fake_poll_device_login(received_challenge):
        assert received_challenge == challenge
        account = _account(7, challenge.label or "browser")
        account.label = challenge.label or account.label
        return LoginResult(account=account, base_url="https://chatgpt.com")

    monkeypatch.setattr(proxy_module, "start_device_login", fake_start_device_login)
    monkeypatch.setattr(proxy_module, "poll_device_login", fake_poll_device_login)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        body = urllib.parse.urlencode({"label": "Team account"}).encode()
        request = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/dashboard/accounts/device/start",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            html = response.read().decode()

        assert response.status == 200
        assert "Finish sign-in" in html
        assert "https://chatgpt.com/device" in html
        assert "ABCD-EFGH" in html
        assert "<html" not in html.lower()

        poll_body = urllib.parse.urlencode(
            {
                "provider_id": challenge.provider_id,
                "device_auth_id": challenge.device_auth_id,
                "user_code": challenge.user_code,
                "verification_url": challenge.verification_url,
                "poll_interval": str(challenge.poll_interval),
                "issuer": challenge.issuer,
                "token_url": challenge.token_url,
                "client_id": challenge.client_id,
                "label": challenge.label or "",
            }
        ).encode()
        poll_request = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/dashboard/accounts/device/poll",
            data=poll_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(poll_request, timeout=5) as response:
            poll_html = response.read().decode()

        assert response.status == 200
        assert "Added Team account via https://chatgpt.com." in poll_html
        assert "Team account" in poll_html
        assert "acct-7" in poll_html
        assert "<html" not in poll_html.lower()

        saved = repo.list_accounts()
        assert len(saved) == 1
        assert saved[0].label == "Team account"
        assert saved[0].provider == "openai-codex"
        assert repo.get_active() is not None
        assert repo.get_active().account_id == "acct-7"
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_can_fallback_across_providers(monkeypatch, tmp_path):
    import sys
    import types
    from dataclasses import dataclass

    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    module = types.ModuleType("test_fake_routing_provider")

    @dataclass
    class FakeAuth:
        provider_id: str = "fake-external"

        def login_via_browser_flow(self, *, label=None):
            raise NotImplementedError

        def login_via_device_flow(self, *, label=None, headless=False):
            raise NotImplementedError

        def start_device_login(self, *, label=None):
            raise NotImplementedError

        def poll_device_login(self, challenge):
            raise NotImplementedError

        def maybe_refresh_account(self, account):
            return account

    @dataclass
    class FakeProxy:
        provider_id: str = "fake-external"

        def local_paths(self):
            return ("/v1/responses",)

        def local_routes(self):
            return (providers_module.LocalRouteSpec(path="/v1/responses", method="POST"),)

        def parse_local_request(self, *, local_path, method, raw_body, headers):
            del headers
            assert local_path == "/v1/responses"
            assert method == "POST"
            return json.loads(raw_body.decode())

        def is_streaming_request(self, payload):
            return bool(payload.get("stream"))

        def matches_request(self, local_path, payload):
            return local_path == "/v1/responses" and payload.get("model") == "gpt-5.4"

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            return providers_module.ProxyRequestSpec(url=self.proxy_upstream_url(local_path), data=b"{}", headers={"X-Test": idempotency_key}, method="POST")

        def proxy_headers(self, account, *, stream, idempotency_key):
            del account, stream
            return {"X-Test": idempotency_key}

        def proxy_extract_usage(self, payload, local_path):
            del payload, local_path
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            del body, local_path
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def create_stream_usage_tracker(self, local_path):
            del local_path

            class Tracker:
                usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

                def feed(self, chunk):
                    del chunk
                    return None

            return Tracker()

        def list_models(self):
            return {"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]}

        def classify_proxy_failure(self, status_code, body):
            del status_code, body
            return False, 0

        def classify_routing_failure(self, status_code, body):
            try:
                payload = json.loads(body.decode())
            except Exception:
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            return providers_module.ProviderRoutingDecision(code in {"no_eligible_accounts", "all_accounts_failed"} or status_code >= 500, "fake-routing")

        def capability_profile(self):
            return providers_module.ProviderCapabilityProfile(model_ids=("gpt-5.4",), supports_streaming=True, supports_tools=True, supports_embeddings=False)

        def describe_local_routes(self, *, base_url):
            return [("Fake responses route", f"{base_url}/v1/responses")]

    descriptor = providers_module.ProviderDescriptor(
        provider_id="fake-external",
        label="Fake External",
        auth=FakeAuth(),
        proxy=FakeProxy(),
    )
    setattr(module, "PROVIDER", descriptor)
    sys.modules[module.__name__] = module
    monkeypatch.setenv("PUNKRECORDS_PROVIDER_MODULES", module.__name__)

    reloaded_providers = importlib.reload(providers_module)
    reloaded_proxy = importlib.reload(proxy_module)
    try:
        settings_store_module.save_settings({"routing": {"provider_order": ["fake-external", "openai-codex"]}})
        repo = AccountRepository()
        repo.upsert_account(_account(1, "work"), make_active=True)

        upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
        _start_server(upstream)
        monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL", f"http://localhost:{upstream.server_port}/responses")
        monkeypatch.setattr(reloaded_providers.require_auth_provider(reloaded_providers.get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

        server = reloaded_proxy.ProxyServer(("localhost", _free_port()), reloaded_proxy.ProxyHandler, repo)
        _start_server(server)
        try:
            request = urllib.request.Request(
                f"http://localhost:{server.server_port}/v1/responses",
                data=json.dumps({"input": "hello", "model": "gpt-5.4"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode())
            assert payload == {"ok": True, "account_id": "acct-1", "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()
    finally:
        monkeypatch.delenv("PUNKRECORDS_PROVIDER_MODULES", raising=False)
        sys.modules.pop(module.__name__, None)
        importlib.reload(reloaded_providers)
        importlib.reload(reloaded_proxy)


def test_proxy_request_transform_plugin_runs_before_upstream(monkeypatch, tmp_path):
    import sys
    import types
    from dataclasses import dataclass

    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    transform_module = types.ModuleType("test_fake_transform_plugin")

    @dataclass
    class PrefixTransform:
        plugin_id: str = "prefix-transform"

        def applies_to(self, payload, context):
            return context.local_path == "/v1/responses"

        def transform(self, payload, context):
            updated = dict(payload)
            updated["input"] = f"prefixed::{payload.get('input', '')}"
            return importlib.import_module("punkrecords.transforms").RequestTransformResult(
                payload=updated,
                applied=True,
                annotations={"provider_id": context.provider_id},
            )

    setattr(transform_module, "REQUEST_TRANSFORM", PrefixTransform())
    sys.modules[transform_module.__name__] = transform_module
    monkeypatch.setenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", transform_module.__name__)

    captured: dict[str, object] = {}

    module = types.ModuleType("test_transform_provider_plugin")

    @dataclass
    class FakeAuth:
        provider_id: str = "fake-transform"

        def login_via_browser_flow(self, *, label=None):
            raise NotImplementedError

        def login_via_device_flow(self, *, label=None, headless=False):
            raise NotImplementedError

        def start_device_login(self, *, label=None):
            raise NotImplementedError

        def poll_device_login(self, challenge):
            raise NotImplementedError

        def maybe_refresh_account(self, account):
            return account

    @dataclass
    class FakeProxy:
        provider_id: str = "fake-transform"

        def local_paths(self):
            return ("/v1/responses",)

        def local_routes(self):
            return (providers_module.LocalRouteSpec(path="/v1/responses", method="POST"),)

        def parse_local_request(self, *, local_path, method, raw_body, headers):
            del local_path, method, headers
            return json.loads(raw_body.decode())

        def is_streaming_request(self, payload):
            return False

        def matches_request(self, local_path, payload):
            return local_path == "/v1/responses" and isinstance(payload.get("input"), str)

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            del account, local_path, idempotency_key
            captured["payload"] = dict(payload)
            return providers_module.ProxyRequestSpec(url=self.proxy_upstream_url("/v1/responses"), data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")

        def proxy_headers(self, account, *, stream, idempotency_key):
            del account, stream, idempotency_key
            return {"Content-Type": "application/json"}

        def proxy_extract_usage(self, payload, local_path):
            del payload, local_path
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            del local_path
            payload = json.loads(body.decode())
            return payload.get("usage", {"input_tokens": None, "output_tokens": None, "total_tokens": None})

        def create_stream_usage_tracker(self, local_path):
            del local_path

            class Tracker:
                usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

                def feed(self, chunk):
                    del chunk
                    return None

            return Tracker()

        def list_models(self):
            return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

        def classify_proxy_failure(self, status_code, body):
            del status_code, body
            return False, 0

        def classify_routing_failure(self, status_code, body):
            del status_code, body
            return providers_module.ProviderRoutingDecision(False, "done")

        def capability_profile(self):
            return providers_module.ProviderCapabilityProfile(model_ids=(), supports_streaming=True, supports_tools=True, supports_embeddings=False)

        def describe_local_routes(self, *, base_url):
            return [("Transform responses route", f"{base_url}/v1/responses")]

    descriptor = providers_module.ProviderDescriptor(
        provider_id="fake-transform",
        label="Fake Transform",
        auth=FakeAuth(),
        proxy=FakeProxy(),
    )
    setattr(module, "PROVIDER", descriptor)
    sys.modules[module.__name__] = module
    monkeypatch.setenv("PUNKRECORDS_PROVIDER_MODULES", module.__name__)

    reloaded_providers = importlib.reload(providers_module)
    reloaded_proxy = importlib.reload(proxy_module)
    try:
        settings_store_module.save_settings({"routing": {"provider_order": ["fake-transform"]}})
        repo = AccountRepository()
        repo.upsert_account(_account(1, "work", provider="fake-transform"), make_active=True)

        upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
        _start_server(upstream)
        monkeypatch.setattr(reloaded_proxy, "_perform_proxy_request", lambda spec, stream=False, timeout=60.0: (200, json.dumps({"ok": True, "echo": json.loads(spec.data.decode())}).encode(), {"Content-Type": "application/json"}))

        server = reloaded_proxy.ProxyServer(("localhost", _free_port()), reloaded_proxy.ProxyHandler, repo)
        _start_server(server)
        try:
            request = urllib.request.Request(
                f"http://localhost:{server.server_port}/v1/responses",
                data=json.dumps({"input": "hello"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode())
            assert payload["echo"]["input"] == "prefixed::hello"
            assert captured["payload"] == {"input": "prefixed::hello"}
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()
    finally:
        monkeypatch.delenv("PUNKRECORDS_PROVIDER_MODULES", raising=False)
        monkeypatch.delenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", raising=False)
        sys.modules.pop(module.__name__, None)
        sys.modules.pop(transform_module.__name__, None)
        importlib.reload(reloaded_providers)
        importlib.reload(reloaded_proxy)


def test_apply_request_transforms_orders_plugins_and_collects_metrics(monkeypatch):
    import sys
    import types
    from dataclasses import dataclass

    transforms_module = importlib.import_module("punkrecords.transforms")
    module = types.ModuleType("test_ordered_transform_plugins")

    @dataclass
    class SuffixTransform:
        plugin_id: str = "suffix"
        order: int = 20
        category: str = transforms_module.REQUEST_TRANSFORM_CATEGORY_RTK
        failure_policy: str = transforms_module.REQUEST_TRANSFORM_FAIL_OPEN
        affects_routing: bool = False

        def applies_to(self, payload, context):
            del context
            return True

        def transform(self, payload, context):
            del context
            updated = dict(payload)
            updated["input"] = f"{payload['input']}::suffix"
            return transforms_module.RequestTransformResult(
                payload=updated,
                applied=True,
                annotations={"stage": "suffix"},
                metrics=transforms_module.RequestTransformMetrics(
                    input_chars_before=5,
                    input_chars_after=13,
                    estimated_tokens_before=4,
                    estimated_tokens_after=3,
                    saved_tokens_estimate=1,
                    input_tokens_saved_estimate=1,
                ),
            )

    @dataclass
    class PrefixTransform:
        plugin_id: str = "prefix"
        order: int = 10
        category: str = transforms_module.REQUEST_TRANSFORM_CATEGORY_PROMPT_POLICY
        failure_policy: str = transforms_module.REQUEST_TRANSFORM_FAIL_OPEN
        affects_routing: bool = True

        def applies_to(self, payload, context):
            del context
            return True

        def transform(self, payload, context):
            del context
            updated = dict(payload)
            updated["input"] = f"prefix::{payload['input']}"
            return transforms_module.RequestTransformResult(
                payload=updated,
                applied=True,
                annotations={"stage": "prefix"},
                affects_routing=True,
                routing_hints={"priority_delta": -10, "reason": "prompt-policy"},
            )

    setattr(module, "REQUEST_TRANSFORMS", [SuffixTransform(), PrefixTransform()])
    sys.modules[module.__name__] = module
    monkeypatch.setenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", module.__name__)
    try:
        result = transforms_module.apply_request_transforms(
            {"input": "hello"},
            transforms_module.RequestTransformContext(
                request_id="req-1",
                local_path="/v1/responses",
                method="POST",
                provider_id="openai-codex",
                headers={"Content-Type": "application/json"},
            ),
        )
        assert result.payload == {"input": "prefix::hello::suffix"}
        assert result.applied is True
        assert result.affects_routing is True
        assert result.routing_hints == {"priority_delta": -10, "reason": "prompt-policy"}
        assert [trace.plugin_id for trace in result.traces] == ["prefix", "suffix"]
        assert result.annotations["plugins"] == ["prefix", "suffix"]
        assert result.annotations["details"]["suffix"]["metrics"]["saved_tokens_estimate"] == 1
        assert result.annotations["details"]["suffix"]["metrics"]["input_tokens_saved_estimate"] == 1
        assert result.annotations["details"]["suffix"]["category"] == transforms_module.REQUEST_TRANSFORM_CATEGORY_RTK
        assert result.annotations["details"]["prefix"]["affects_routing"] is True
        assert result.annotations["details"]["prefix"]["routing_hints"] == {"priority_delta": -10, "reason": "prompt-policy"}
    finally:
        monkeypatch.delenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", raising=False)
        sys.modules.pop(module.__name__, None)


def test_apply_request_transforms_fail_open_continues(monkeypatch):
    import sys
    import types
    from dataclasses import dataclass

    transforms_module = importlib.import_module("punkrecords.transforms")
    module = types.ModuleType("test_fail_open_transform_plugins")

    @dataclass
    class BrokenTransform:
        plugin_id: str = "broken"
        order: int = 10
        failure_policy: str = transforms_module.REQUEST_TRANSFORM_FAIL_OPEN

        def applies_to(self, payload, context):
            del payload, context
            return True

        def transform(self, payload, context):
            del payload, context
            raise RuntimeError("boom")

    @dataclass
    class WorkingTransform:
        plugin_id: str = "working"
        order: int = 20

        def applies_to(self, payload, context):
            del context
            return True

        def transform(self, payload, context):
            del context
            updated = dict(payload)
            updated["input"] = f"{payload['input']}::ok"
            return transforms_module.RequestTransformResult(payload=updated, applied=True)

    setattr(module, "REQUEST_TRANSFORMS", [WorkingTransform(), BrokenTransform()])
    sys.modules[module.__name__] = module
    monkeypatch.setenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", module.__name__)
    try:
        result = transforms_module.apply_request_transforms(
            {"input": "hello"},
            transforms_module.RequestTransformContext(
                request_id="req-2",
                local_path="/v1/responses",
                method="POST",
                provider_id="openai-codex",
                headers={"Content-Type": "application/json"},
            ),
        )
        assert result.payload == {"input": "hello::ok"}
        assert result.annotations["details"]["broken"]["error"] == "boom"
        assert result.annotations["plugins"] == ["working"]
    finally:
        monkeypatch.delenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", raising=False)
        sys.modules.pop(module.__name__, None)


def test_proxy_request_transform_fail_closed_returns_error(monkeypatch, tmp_path):
    import sys
    import types
    from dataclasses import dataclass

    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    transforms_module = importlib.import_module("punkrecords.transforms")
    transform_module = types.ModuleType("test_fail_closed_transform_plugin")

    @dataclass
    class FailClosedTransform:
        plugin_id: str = "guard"
        failure_policy: str = transforms_module.REQUEST_TRANSFORM_FAIL_CLOSED
        category: str = transforms_module.REQUEST_TRANSFORM_CATEGORY_REDACTION

        def applies_to(self, payload, context):
            del payload, context
            return True

        def transform(self, payload, context):
            del payload, context
            raise RuntimeError("blocked by guard")

    setattr(transform_module, "REQUEST_TRANSFORM", FailClosedTransform())
    sys.modules[transform_module.__name__] = transform_module
    monkeypatch.setenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", transform_module.__name__)

    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)
    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hello", "model": "gpt-5.4"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
            payload = json.loads(exc.read().decode())
            assert payload["error"]["code"] == "request_transform_failed"
            assert "guard" in payload["error"]["message"]
        else:
            raise AssertionError("Expected transform failure response")
    finally:
        proxy.shutdown()
        proxy.server_close()
        monkeypatch.delenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", raising=False)
        sys.modules.pop(transform_module.__name__, None)


def test_transform_categories_are_explicit_for_future_plugins():
    transforms_module = importlib.import_module("punkrecords.transforms")

    assert transforms_module.REQUEST_TRANSFORM_CATEGORY_RTK == "rtk"
    assert transforms_module.REQUEST_TRANSFORM_CATEGORY_REDACTION == "redaction"
    assert transforms_module.REQUEST_TRANSFORM_CATEGORY_SUMMARIZATION == "summarization"
    assert transforms_module.REQUEST_TRANSFORM_CATEGORY_PROMPT_POLICY == "prompt-policy"


def test_proxy_embeddings_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/embeddings",
            data=json.dumps({"input": "hello", "model": "text-embedding-3-small"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["model"] == "text-embedding-3-small"
        assert payload["usage"] == {"prompt_tokens": 6, "total_tokens": 6}

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 6
        assert stats["output_tokens"] == 0
        assert stats["total_tokens"] == 6
        assert stats["by_endpoint"]["/v1/embeddings"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_embeddings_failover_to_second_account(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    repo.upsert_account(_account(2, "backup"), make_active=False)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), EmbeddingsFailoverHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/embeddings",
            data=json.dumps({"input": "hello", "model": "text-embedding-3-small"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["model"] == "text-embedding-3-small"

        active = repo.get_active()
        assert active is not None
        assert active.account_id == "acct-2"

        accounts = repo.list_accounts()
        first = next(account for account in accounts if account.account_id == "acct-1")
        assert first.cooldown_until is not None
        assert first.last_proxy_error is not None

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["by_account"]["openai-codex:acct-2"] == 1
        assert stats["by_provider_account"]["openai-codex:acct-2"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_stats_summary_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/stats/summary", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload == {
            "request_count": 0,
            "success_count": 0,
            "error_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "by_endpoint": {},
            "by_account": {},
            "by_provider_account": {},
            "updated_at": None,
        }
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_admin_state_and_accounts(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    repo.upsert_account(_account(2, "backup"), make_active=False)
    repo.mark_proxy_failure("acct-2", error="deactivated_workspace", cooldown_seconds=300)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/state", timeout=5) as response:
            state = json.loads(response.read().decode())
        assert state["ok"] is True
        assert state["accounts_total"] == 2
        assert state["accounts_enabled"] == 2
        assert state["eligible_accounts"] == 1
        assert state["cooldown_accounts"] == 1
        assert state["active_account_id"] == "acct-1"
        assert "stats" in state
        assert "settings" in state

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/accounts", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert len(payload["data"]) == 2
        assert all("tokens" not in account for account in payload["data"])
        backup = next(account for account in payload["data"] if account["account_id"] == "acct-2")
        assert backup["eligible"] is False
        assert backup["last_proxy_error"] == "deactivated_workspace"
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_admin_requests_and_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        request = urllib.request.Request(
            f"http://localhost:{server.server_port}/v1/responses",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5):
            pass

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/requests?limit=10", timeout=5) as response:
            history = json.loads(response.read().decode())
        assert len(history["data"]) == 1
        assert history["data"][0]["endpoint"] == "/v1/responses"

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/settings", timeout=5) as response:
            settings = json.loads(response.read().decode())
        assert settings["proxy"]["port"] == 4141

        dashboard_update = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/dashboard/settings",
            data=urllib.parse.urlencode(
                {
                    "proxy_host": "0.0.0.0",
                    "proxy_port": "5002",
                    "proxy_max_attempts": "4",
                    "routing_provider_order": "openai-codex",
                    "routing_route_overrides": "{}",
                    "routing_model_overrides": "{}",
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(dashboard_update, timeout=5) as response:
            settings_fragment = response.read().decode()
        assert "Settings saved." in settings_fragment
        assert "value=\"5002\"" in settings_fragment

        bad_dashboard_update = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/dashboard/settings",
            data=urllib.parse.urlencode(
                {
                    "proxy_host": "0.0.0.0",
                    "proxy_port": "not-a-port",
                    "proxy_max_attempts": "4",
                    "routing_provider_order": "openai-codex",
                    "routing_route_overrides": "{}",
                    "routing_model_overrides": "{}",
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(bad_dashboard_update, timeout=5) as response:
            settings_error_fragment = response.read().decode()
        assert "settings.proxy.port must be an integer between 1 and 65535" in settings_error_fragment
        assert "value=\"not-a-port\"" in settings_error_fragment

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/settings", timeout=5) as response:
            dashboard_saved_settings = json.loads(response.read().decode())
        assert dashboard_saved_settings["proxy"]["port"] == 5002
        assert dashboard_saved_settings["proxy"]["max_attempts"] == 4

        with urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/dashboard/requests?limit=10", timeout=5) as response:
            requests_fragment = response.read().decode()
        assert "/v1/responses" in requests_fragment
        assert "<html" not in requests_fragment.lower()

        update = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/admin/settings",
            data=json.dumps({"proxy": {"port": 5001}}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(update, timeout=5) as response:
            updated = json.loads(response.read().decode())
        assert updated["proxy"]["port"] == 5001

        invalid = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/admin/settings",
            data=json.dumps({"proxy": {"port": 70000}}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            urllib.request.urlopen(invalid, timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 400
            assert payload["error"]["code"] == "invalid_settings"

        invalid_key = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/admin/settings",
            data=json.dumps({"proxy": {"unknown": True}}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            urllib.request.urlopen(invalid_key, timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 400
            assert payload["error"]["code"] == "invalid_settings"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_admin_auth_token(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("PUNKRECORDS_ADMIN_TOKEN", "secret-token")
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        try:
            urllib.request.urlopen(f"http://localhost:{server.server_port}/_proxy/admin/state", timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 401
            assert payload["error"]["code"] == "admin_auth_required"

        request = urllib.request.Request(
            f"http://localhost:{server.server_port}/_proxy/admin/state",
            headers={"Authorization": "Bearer secret-token"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_error_envelope_and_method_handling(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        bad_json_request = urllib.request.Request(
            f"http://localhost:{server.server_port}/v1/responses",
            data=b"{bad",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_json_request, timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 400
            assert payload["error"]["code"] == "invalid_json"
            assert payload["error"]["type"] == "invalid_request_error"

        get_request = urllib.request.Request(f"http://localhost:{server.server_port}/v1/responses", method="GET")
        try:
            urllib.request.urlopen(get_request, timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 405
            assert exc.headers["Allow"] == "POST"
            assert payload["error"]["code"] == "method_not_allowed"

    finally:
        server.shutdown()
        server.server_close()


def test_proxy_refresh_failure_returns_controlled_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: (_ for _ in ()).throw(OAuthError("refresh failed")))

    server = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(server)
    try:
        request = urllib.request.Request(
            f"http://localhost:{server.server_port}/v1/responses",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 503
            assert payload["error"]["code"] == "account_refresh_failed"
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_failover_to_second_account(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    repo.upsert_account(_account(2, "backup"), make_active=False)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), FakeUpstreamHandler)
    _start_server(upstream)

    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL", f"http://localhost:{upstream.server_port}/responses")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"ok": True, "account_id": "acct-2"}

        active = repo.get_active()
        assert active is not None
        assert active.account_id == "acct-2"

        accounts = repo.list_accounts()
        first = next(account for account in accounts if account.account_id == "acct-1")
        assert first.cooldown_until is not None
        assert first.last_proxy_error is not None
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_persists_refreshed_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL", f"http://localhost:{upstream.server_port}/responses")

    def fake_refresh(account):
        account.tokens = models_module.AccountTokens(
            access_token="rotated-token",
            refresh_token="rotated-refresh",
            account_id=account.account_id,
        )
        return account

    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", fake_refresh)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"ok": True, "account_id": "acct-1", "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}

        stored = repo.get_active()
        assert stored is not None
        assert stored.tokens.access_token == "rotated-token"
        assert stored.tokens.refresh_token == "rotated-refresh"
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_responses_non_stream_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hi"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"ok": True, "account_id": "acct-1", "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 12
        assert stats["output_tokens"] == 8
        assert stats["total_tokens"] == 20
        assert stats["by_endpoint"]["/v1/responses"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_chat_completions_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["role"] == "assistant"
        assert payload["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 12
        assert stats["output_tokens"] == 8
        assert stats["total_tokens"] == 20
        assert stats["by_endpoint"]["/v1/chat/completions"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_streaming_passthrough_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), StreamingUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()
            content_type = response.headers.get("Content-Type")

        assert content_type == "text/event-stream"
        assert 'event: response.output_text.delta' in body
        assert 'event: response.completed' in body

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 9
        assert stats["output_tokens"] == 4
        assert stats["total_tokens"] == 13
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_responses_streaming_passthrough_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("localhost", _free_port()), StreamingUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", f"http://localhost:{upstream.server_port}")
    monkeypatch.setattr(providers_module.require_auth_provider(get_provider("openai-codex")), "maybe_refresh_account", lambda account: account)

    proxy = ProxyServer(("localhost", _free_port()), ProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://localhost:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hi", "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()
            content_type = response.headers.get("Content-Type")

        assert content_type == "text/event-stream"
        assert 'event: response.created' in body
        assert 'event: response.output_text.delta' in body
        assert 'event: response.completed' in body

        with urllib.request.urlopen(f"http://localhost:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 9
        assert stats["output_tokens"] == 4
        assert stats["total_tokens"] == 13
        assert stats["by_endpoint"]["/v1/responses"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
