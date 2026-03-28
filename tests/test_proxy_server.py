from __future__ import annotations

import importlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

models_module = importlib.import_module("hermes_codex_multi_auth.models")
oauth_module = importlib.import_module("hermes_codex_multi_auth.oauth")
proxy_module = importlib.import_module("hermes_codex_multi_auth.proxy")
store_module = importlib.import_module("hermes_codex_multi_auth.store")

AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
OAuthError = oauth_module.OAuthError
CodexProxyHandler = proxy_module.CodexProxyHandler
CodexProxyServer = proxy_module.CodexProxyServer


def _account(index: int, label: str) -> AccountRecord:
    return AccountRecord(
        id=f"local-{index}",
        account_id=f"acct-{index}",
        email=f"user{index}@example.com",
        label=label,
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
    sock.bind(("127.0.0.1", 0))
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
        if self.path == "/chat/completions":
            payload = {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
            body = json.dumps(payload).encode()
        elif self.path == "/embeddings":
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
        if self.path == "/chat/completions":
            body = (
                'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hel"},"index":0}],"usage":null}\n\n'
                'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
                'data: [DONE]\n\n'
            ).encode()
        else:
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
            probe.connect(("127.0.0.1", server.server_port))
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

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=5) as response:
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

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/models", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["object"] == "list"
        assert [item["id"] for item in payload["data"]] == ["gpt-5.4", "gpt-5.4-mini"]
    finally:
        server.shutdown()
        server.server_close()


def test_proxy_embeddings_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/embeddings",
            data=json.dumps({"input": "hello", "model": "text-embedding-3-small"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["model"] == "text-embedding-3-small"
        assert payload["usage"] == {"prompt_tokens": 6, "total_tokens": 6}

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), EmbeddingsFailoverHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/embeddings",
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

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["by_account"]["acct-2"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_stats_summary_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/stats/summary", timeout=5) as response:
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

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/admin/state", timeout=5) as response:
            state = json.loads(response.read().decode())
        assert state["ok"] is True
        assert state["accounts_total"] == 2
        assert state["accounts_enabled"] == 2
        assert state["eligible_accounts"] == 1
        assert state["cooldown_accounts"] == 1
        assert state["active_account_id"] == "acct-1"
        assert "stats" in state
        assert "settings" in state

        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/admin/accounts", timeout=5) as response:
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5):
            pass

        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/admin/requests?limit=10", timeout=5) as response:
            history = json.loads(response.read().decode())
        assert len(history["data"]) == 1
        assert history["data"][0]["endpoint"] == "/v1/responses"

        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/admin/settings", timeout=5) as response:
            settings = json.loads(response.read().decode())
        assert settings["proxy"]["port"] == 4141

        update = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/_proxy/admin/settings",
            data=json.dumps({"proxy": {"port": 5001}, "dashboard": {"enabled": True}}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(update, timeout=5) as response:
            updated = json.loads(response.read().decode())
        assert updated["proxy"]["port"] == 5001
        assert updated["dashboard"]["enabled"] is True

        invalid = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/_proxy/admin/settings",
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
            f"http://127.0.0.1:{server.server_port}/_proxy/admin/settings",
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
    monkeypatch.setenv("HERMES_CODEX_ADMIN_TOKEN", "secret-token")
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/_proxy/admin/state", timeout=5)
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            assert exc.code == 401
            assert payload["error"]["code"] == "admin_auth_required"

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/_proxy/admin/state",
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

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        bad_json_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses",
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

        get_request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/responses", method="GET")
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

    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: (_ for _ in ()).throw(OAuthError("refresh failed")))

    server = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(server)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses",
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), FakeUpstreamHandler)
    _start_server(upstream)

    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_URL", f"http://127.0.0.1:{upstream.server_port}/responses")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/responses",
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_URL", f"http://127.0.0.1:{upstream.server_port}/responses")

    def fake_refresh(account):
        account.tokens.access_token = "rotated-token"
        account.tokens.refresh_token = "rotated-refresh"
        return account

    monkeypatch.setattr(proxy_module, "maybe_refresh_account", fake_refresh)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/responses",
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/responses",
            data=json.dumps({"input": "hi"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"ok": True, "account_id": "acct-1", "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SuccessUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 11
        assert stats["output_tokens"] == 7
        assert stats["total_tokens"] == 18
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

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), StreamingUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()
            content_type = response.headers.get("Content-Type")

        assert content_type == "text/event-stream"
        assert 'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hel"},"index":0}],"usage":null}' in body
        assert 'data: [DONE]' in body

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
            stats = json.loads(response.read().decode())
        assert stats["request_count"] == 1
        assert stats["input_tokens"] == 10
        assert stats["output_tokens"] == 5
        assert stats["total_tokens"] == 15
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_responses_streaming_passthrough_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), StreamingUpstreamHandler)
    _start_server(upstream)
    monkeypatch.setenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.server_port}")
    monkeypatch.setattr(proxy_module, "maybe_refresh_account", lambda account: account)

    proxy = CodexProxyServer(("127.0.0.1", _free_port()), CodexProxyHandler, repo)
    _start_server(proxy)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/responses",
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

        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/_proxy/stats/summary", timeout=5) as response:
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
