from __future__ import annotations

import importlib
import json
import socket
import threading
import urllib.request

mock_module = importlib.import_module("tests.mock_oauth_server")
MockOAuthServer = mock_module.MockOAuthServer
OAuthHandler = mock_module.OAuthHandler

cli_module = importlib.import_module("punkrecords.cli")
providers_module = importlib.import_module("punkrecords.providers")
main = cli_module.main
get_provider = providers_module.get_provider


def _start_server() -> tuple[MockOAuthServer, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    sock.close()

    server = MockOAuthServer(("127.0.0.1", port), OAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_headless_login_and_multi_account_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    server, port = _start_server()
    issuer = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_TOKEN_URL", f"{issuer}/oauth/token")
    try:
        assert main(["login", "--headless", "--label", "work"]) == 0
        output1 = capsys.readouterr().out
        assert "Saved account: work" in output1
        assert "Account id:    acct-1" in output1

        assert main(["login", "--headless", "--label", "backup"]) == 0
        output2 = capsys.readouterr().out
        assert "Saved account: backup" in output2
        assert "Account id:    acct-2" in output2

        assert main(["list", "--json"]) == 0
        accounts = json.loads(capsys.readouterr().out)
        assert len(accounts) == 2
        assert accounts[0]["label"] == "work"
        assert accounts[1]["label"] == "backup"
        assert accounts[1]["active"] is True

        assert main(["status", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["account_count"] == 2
        assert status["active_account"]["account_id"] == "acct-2"
    finally:
        server.shutdown()
        server.server_close()


def test_browser_login_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    server, port = _start_server()
    issuer = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("PUNKRECORDS_OPENAI_CODEX_OAUTH_TOKEN_URL", f"{issuer}/oauth/token")

    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        urllib.request.urlopen(url, timeout=5).read()
        return True

    monkeypatch.setattr("punkrecords.providers.openai_codex.webbrowser.open", fake_open)
    try:
        assert main(["login", "--label", "browser"]) == 0
        output = capsys.readouterr().out
        assert opened and opened[0].startswith(f"{issuer}/oauth/authorize?")
        assert "Authorization URL:" in output
        assert "Opened browser automatically." in output
        assert "Saved account: browser" in output
        assert "Account id:    acct-101" in output
    finally:
        server.shutdown()
        server.server_close()


def test_login_command_uses_registry_provider(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    provider = providers_module.require_auth_provider(get_provider("openai-codex"))

    def fake_login_via_device_flow(*, label=None, headless=False):
        assert label == "registry"
        assert headless is True
        return providers_module.LoginResult(
            account=importlib.import_module("punkrecords.models").AccountRecord(
                id="local-registry",
                account_id="acct-registry",
                email="registry@example.com",
                label="registry",
                provider="openai-codex",
                created_at="2026-03-27T00:00:00Z",
                last_refresh="2026-03-27T00:00:00Z",
                last_used="2026-03-27T00:00:00Z",
                tokens=importlib.import_module("punkrecords.models").AccountTokens(
                    access_token="access",
                    refresh_token="refresh",
                    account_id="acct-registry",
                ),
            ),
            base_url="https://example.test/codex",
        )

    monkeypatch.setattr(provider, "login_via_device_flow", fake_login_via_device_flow)

    assert main(["login", "--headless", "--label", "registry"]) == 0
    output = capsys.readouterr().out
    assert "Saved account: registry" in output
    assert "Account id:    acct-registry" in output


def test_help_command(capsys):
    assert main(["help"]) == 0
    output = capsys.readouterr().out
    assert "PunkRecords" in output
    assert "Primary CLI: punkrecords" in output
    assert "Default runtime paths:" in output
    assert "runtime root:" in output
    assert "Recommended flow:" in output
    assert "uv run punkrecords proxy --host 127.0.0.1 --port 4141" in output
    assert "uv run punkrecords login --label work" in output
