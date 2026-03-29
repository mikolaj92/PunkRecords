from __future__ import annotations

import base64
import importlib
import json
import time
from dataclasses import dataclass
from typing import Mapping

cli_module = importlib.import_module("punkrecords.cli")
models_module = importlib.import_module("punkrecords.models")
paths_module = importlib.import_module("punkrecords.paths")
providers_module = importlib.import_module("punkrecords.providers")
store_module = importlib.import_module("punkrecords.store")
usage_module = importlib.import_module("punkrecords.models")

main = cli_module.main
AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
AccountUsage = usage_module.AccountUsage
UsageWindow = usage_module.UsageWindow
get_provider = providers_module.get_provider


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

    assert main(["status"]) == 0
    status_output = capsys.readouterr().out
    assert "Accounts:" in status_output
    assert "Active:        none" in status_output

    assert main(["list"]) == 0
    list_output = capsys.readouterr().out
    assert "No accounts saved yet." in list_output


def test_switch_changes_active_account(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    repo = AccountRepository()
    repo.upsert_account(make_account("acct-1", "work", "work@example.com"), make_active=True)
    repo.upsert_account(make_account("acct-2", "backup", "backup@example.com"), make_active=False)

    assert main(["switch", "2"]) == 0
    switch_output = capsys.readouterr().out
    assert "Active account: backup" in switch_output


def test_repository_load_migrates_missing_provider_to_legacy_builtin(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_account_id": "local-acct-legacy",
                "accounts": [
                    {
                        "id": "local-acct-legacy",
                        "account_id": "acct-legacy",
                        "email": "legacy@example.com",
                        "label": "legacy",
                        "tokens": {
                            "access_token": "access",
                            "refresh_token": "refresh",
                            "account_id": "acct-legacy",
                        },
                    }
                ],
            }
        )
    )

    repo = AccountRepository(path)
    account = repo.list_accounts()[0]
    assert account.provider == "openai-codex"


def test_provider_registry_exposes_builtin_openai_codex():
    provider = get_provider(None)

    assert provider.provider_id == "openai-codex"
    assert provider.label == "OpenAI Codex"
    assert providers_module.supported_provider_metadata() == [{"id": "openai-codex", "label": "OpenAI Codex"}]


def test_provider_registry_can_load_external_provider(monkeypatch):
    import sys
    import types

    module = types.ModuleType("test_fake_provider_plugin")

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
            account.provider_state = {**account.provider_state, "fake": "refreshed"}
            return account

        def fetch_account_usage(self, account, timeout=15.0):
            return account, AccountUsage(account_id=account.account_id, label=account.label, provider=account.provider, plan_type="fake")

        def usage_url(self):
            return "https://example.invalid/usage"

        def local_paths(self):
            return ("/v1/fake",)

        def local_routes(self):
            return (providers_module.LocalRouteSpec(path="/v1/fake", method="POST"),)

        def parse_local_request(self, *, local_path, method, raw_body, headers):
            return {"ok": True, "provider": self.provider_id}

        def is_streaming_request(self, payload):
            return False

        def matches_request(self, local_path, payload):
            return local_path == "/v1/fake" and payload.get("provider") == self.provider_id

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            return providers_module.ProxyRequestSpec(url=self.proxy_upstream_url(local_path), data=b"{}", headers={"X-Test": idempotency_key}, method="POST")

        def proxy_headers(self, account, *, stream, idempotency_key):
            return {"X-Test": idempotency_key}

        def proxy_extract_usage(self, payload, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def create_stream_usage_tracker(self, local_path):
            class Tracker:
                usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

                def feed(self, chunk):
                    return None

            return Tracker()

        def list_models(self):
            return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

        def classify_proxy_failure(self, status_code, body):
            return False, 0

        def build_provider_state(self, account):
            return {"fake_state": account.provider_state}

        def extract_provider_identity(self, payload):
            return None

    @dataclass
    class FakeUsage:
        provider_id: str = "fake-external"

        def fetch_account_usage(self, account, timeout=15.0):
            return account, AccountUsage(account_id=account.account_id, label=account.label, provider=account.provider, plan_type="fake")

        def usage_url(self):
            return "https://example.invalid/usage"

    @dataclass
    class FakeProxy:
        provider_id: str = "fake-external"

        def local_paths(self):
            return ("/v1/fake",)

        def local_routes(self):
            return (providers_module.LocalRouteSpec(path="/v1/fake", method="POST"),)

        def parse_local_request(self, *, local_path, method, raw_body, headers):
            return {"ok": True, "provider": self.provider_id}

        def is_streaming_request(self, payload):
            return False

        def matches_request(self, local_path, payload):
            return local_path == "/v1/fake" and payload.get("provider") == self.provider_id

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            return providers_module.ProxyRequestSpec(url=self.proxy_upstream_url(local_path), data=b"{}", headers={"X-Test": idempotency_key}, method="POST")

        def proxy_headers(self, account, *, stream, idempotency_key):
            return {"X-Test": idempotency_key}

        def proxy_extract_usage(self, payload, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def create_stream_usage_tracker(self, local_path):
            class Tracker:
                usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

                def feed(self, chunk):
                    return None

            return Tracker()

        def list_models(self):
            return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

        def classify_proxy_failure(self, status_code, body):
            return False, 0

    descriptor = providers_module.ProviderDescriptor(
        provider_id="fake-external",
        label="Fake External",
        auth=FakeAuth(),
        usage=FakeUsage(),
        proxy=FakeProxy(),
    )

    setattr(module, "PROVIDER", descriptor)
    sys.modules[module.__name__] = module
    monkeypatch.setenv("PUNKRECORDS_PROVIDER_MODULES", module.__name__)

    reloaded = importlib.reload(providers_module)
    try:
        provider = reloaded.get_provider("fake-external")
        assert provider.provider_id == "fake-external"
        assert any(item["id"] == "fake-external" for item in reloaded.supported_provider_metadata())
        record = make_account("acct-fake", "fake", "fake@example.com")
        record.provider = "fake-external"
        record.provider_state = {"fake": "initial"}
        record.tokens = AccountTokens("", "", "")
        refreshed = reloaded.require_auth_provider(provider).maybe_refresh_account(record)
        assert refreshed.provider_state["fake"] == "refreshed"
        assert reloaded.require_proxy_provider(provider).local_routes()[0].path == "/v1/fake"
        assert reloaded.require_proxy_provider(provider).list_models()["data"][0]["id"] == "fake-model"
    finally:
        monkeypatch.delenv("PUNKRECORDS_PROVIDER_MODULES", raising=False)
        sys.modules.pop(module.__name__, None)
        importlib.reload(reloaded)


def test_repository_treats_same_account_id_from_different_providers_as_distinct(tmp_path):
    repo = AccountRepository(tmp_path / "accounts.json")
    first = make_account("acct-shared", "one", "one@example.com")
    second = make_account("acct-shared", "two", "two@example.com")
    second.provider = "other-provider"

    repo.upsert_account(first, make_active=True)
    repo.upsert_account(second, make_active=False)

    accounts = repo.list_accounts()
    assert len(accounts) == 2
    assert {account.provider for account in accounts} == {"openai-codex", "other-provider"}


def test_status_aggregates_usage(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))

    repo = AccountRepository()
    repo.upsert_account(make_account("acct-1", "work", "work@example.com"), make_active=True)
    repo.upsert_account(make_account("acct-2", "backup", "backup@example.com"), make_active=False)

    def fake_fetch(account):
        if account.account_id == "acct-1":
            return account, AccountUsage(
                account_id=account.account_id,
                label=account.label,
                provider=account.provider,
                primary_window=UsageWindow(used_percent=25.0, reset_after_seconds=100, reset_at=1760000100),
                secondary_window=UsageWindow(used_percent=10.0, reset_after_seconds=1000, reset_at=1760001000),
            )
        return account, AccountUsage(
            account_id=account.account_id,
            label=account.label,
            provider=account.provider,
            primary_window=UsageWindow(used_percent=40.0, reset_after_seconds=200, reset_at=1760000200),
            secondary_window=UsageWindow(used_percent=15.0, reset_after_seconds=1200, reset_at=1760002000),
        )

    monkeypatch.setattr(cli_module, "get_fetch_account_usage", lambda: fake_fetch)

    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "Usage" in output
    assert "| Account | Provider     | Plan" in output
    assert "openai-codex | unknown | 25.0%" in output
    assert "openai-codex | unknown | 40.0%" in output
    assert "Summary:" in output
    assert "Reported 5h:   65.0% / 200.0% across 2 account(s), reset in 1m 40s" in output
    assert "Reported week: 25.0% / 200.0% across 2 account(s), reset in 16m 40s" in output

    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["usage_reports"]) == 1
    report = payload["usage_reports"][0]
    assert report["summary"]["5h"]["used_percent_total"] == 65.0
    assert report["summary"]["weekly"]["used_percent_total"] == 25.0
    assert report["summary"]["5h"]["reset_after_seconds_min"] == 100
    assert report["summary"]["weekly"]["reset_after_seconds_min"] == 1000


def test_app_home_prefers_new_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "punk-home"))

    assert paths_module.app_home() == tmp_path / "punk-home"


def test_app_home_uses_current_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"


def test_app_home_defaults_to_repo_local_directory(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"
