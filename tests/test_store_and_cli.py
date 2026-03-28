from __future__ import annotations

import base64
import importlib
import json
import time
from typing import Mapping

cli_module = importlib.import_module("hermes_codex_multi_auth.cli")
models_module = importlib.import_module("hermes_codex_multi_auth.models")
paths_module = importlib.import_module("hermes_codex_multi_auth.paths")
store_module = importlib.import_module("hermes_codex_multi_auth.store")
usage_module = importlib.import_module("hermes_codex_multi_auth.models")

main = cli_module.main
AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
AccountUsage = usage_module.AccountUsage
UsageWindow = usage_module.UsageWindow


def _jwt_segment(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _access_token(account_id: str, email: str) -> str:
    payload = {
        "exp": int(time.time()) + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "https://api.openai.com/profile": {"email": email},
    }
    return f"header.{_jwt_segment(payload)}.sig"


def make_account(account_id: str, label: str, email: str) -> AccountRecord:
    return AccountRecord(
        id=f"local-{account_id}",
        account_id=account_id,
        email=email,
        label=label,
        created_at="2026-03-27T00:00:00Z",
        last_refresh="2026-03-27T00:00:00Z",
        last_used="2026-03-27T00:00:00Z",
        tokens=AccountTokens(
            access_token=_access_token(account_id, email),
            refresh_token=f"refresh-{account_id}",
            account_id=account_id,
        ),
    )


def test_status_and_list_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    assert main(["status"]) == 0
    status_output = capsys.readouterr().out
    assert "Accounts:" in status_output
    assert "Active:        none" in status_output

    assert main(["list"]) == 0
    list_output = capsys.readouterr().out
    assert "No accounts saved yet." in list_output


def test_switch_and_sync(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    repo = AccountRepository()
    repo.upsert_account(make_account("acct-1", "work", "work@example.com"), make_active=True)
    repo.upsert_account(make_account("acct-2", "backup", "backup@example.com"), make_active=False)

    assert main(["switch", "2"]) == 0
    switch_output = capsys.readouterr().out
    assert "Active account: backup" in switch_output

    assert main(["sync"]) == 0
    sync_output = capsys.readouterr().out
    assert "Synced account backup" in sync_output

    auth_path = tmp_path / "hermes" / "auth.json"
    payload = json.loads(auth_path.read_text())
    provider = payload["providers"]["openai-codex"]
    assert provider["tokens"]["account_id"] == "acct-2"
    assert payload["active_provider"] == "openai-codex"


def test_status_aggregates_usage(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    repo = AccountRepository()
    repo.upsert_account(make_account("acct-1", "work", "work@example.com"), make_active=True)
    repo.upsert_account(make_account("acct-2", "backup", "backup@example.com"), make_active=False)

    def fake_fetch(account):
        if account.account_id == "acct-1":
            return account, AccountUsage(
                account_id=account.account_id,
                label=account.label,
                primary_window=UsageWindow(used_percent=25.0, reset_after_seconds=100, reset_at=1760000100),
                secondary_window=UsageWindow(used_percent=10.0, reset_after_seconds=1000, reset_at=1760001000),
            )
        return account, AccountUsage(
            account_id=account.account_id,
            label=account.label,
            primary_window=UsageWindow(used_percent=40.0, reset_after_seconds=200, reset_at=1760000200),
            secondary_window=UsageWindow(used_percent=15.0, reset_after_seconds=1200, reset_at=1760002000),
        )

    monkeypatch.setattr(cli_module, "get_fetch_account_usage", lambda: fake_fetch)

    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "Per-account usage:" in output
    assert "| Account | 5h    | 5h reset" in output
    assert "| work    | 25.0% | reset in 1m 40s at 2025-10-09 08:55:00Z" in output
    assert "| backup  | 40.0% | reset in 3m 20s at 2025-10-09 08:56:40Z" in output
    assert "Summary:" in output
    assert "Reported 5h:   65.0% / 200.0% across 2 account(s), reset in 1m 40s" in output
    assert "Reported week: 25.0% / 200.0% across 2 account(s), reset in 16m 40s" in output

    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["usage_totals"]["5h"]["used_percent_total"] == 65.0
    assert payload["usage_totals"]["weekly"]["used_percent_total"] == 25.0
    assert payload["usage_totals"]["5h"]["reset_after_seconds_min"] == 100
    assert payload["usage_totals"]["weekly"]["reset_after_seconds_min"] == 1000


def test_app_home_prefers_new_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "punk-home"))
    monkeypatch.setenv("HERMES_CODEX_MULTI_AUTH_HOME", str(tmp_path / "legacy-home"))

    assert paths_module.app_home() == tmp_path / "punk-home"


def test_app_home_supports_legacy_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)
    monkeypatch.setenv("HERMES_CODEX_MULTI_AUTH_HOME", str(tmp_path / "legacy-home"))

    assert paths_module.app_home() == tmp_path / "legacy-home"


def test_app_home_defaults_to_repo_local_directory(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)
    monkeypatch.delenv("HERMES_CODEX_MULTI_AUTH_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"


def test_hermes_auth_path_defaults_under_repo_local_runtime(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)
    monkeypatch.delenv("HERMES_CODEX_MULTI_AUTH_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert paths_module.hermes_auth_path() == paths_module.project_root() / ".punkrecords" / "hermes" / "auth.json"


def test_hermes_auth_path_supports_hermes_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    assert paths_module.hermes_auth_path() == tmp_path / "hermes-home" / "auth.json"
