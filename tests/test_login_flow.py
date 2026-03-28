from __future__ import annotations

import importlib
import json
import socket
import threading
import urllib.request

mock_module = importlib.import_module("tests.mock_oauth_server")
MockOAuthServer = mock_module.MockOAuthServer
OAuthHandler = mock_module.OAuthHandler

cli_module = importlib.import_module("hermes_codex_multi_auth.cli")
main = cli_module.main


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
    monkeypatch.setenv("HERMES_CODEX_MULTI_AUTH_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    server, port = _start_server()
    issuer = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HERMES_CODEX_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("HERMES_CODEX_OAUTH_TOKEN_URL", f"{issuer}/oauth/token")
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
    monkeypatch.setenv("HERMES_CODEX_MULTI_AUTH_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    server, port = _start_server()
    issuer = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HERMES_CODEX_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("HERMES_CODEX_OAUTH_TOKEN_URL", f"{issuer}/oauth/token")

    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        urllib.request.urlopen(url, timeout=5).read()
        return True

    monkeypatch.setattr("hermes_codex_multi_auth.oauth.webbrowser.open", fake_open)
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


def test_help_command(capsys):
    assert main(["help"]) == 0
    output = capsys.readouterr().out
    assert "PunkRecords" in output
    assert "Primary CLI: punkrecords" in output
    assert "Compatibility CLI: hermes-codex-auth" in output
    assert "Default runtime paths:" in output
    assert "runtime root:" in output
    assert ".punkrecords/hermes/auth.json" in output
    assert "Recommended flow:" in output
    assert "uv run punkrecords proxy --host 127.0.0.1 --port 4141" in output
    assert "uv run punkrecords login --label work" in output
    assert "uv run punkrecords sync" in output
    assert "hermes-codex-auth remains available as an alias" in output
