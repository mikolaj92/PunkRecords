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
settings_store_module = importlib.import_module("punkrecords.settings_store")
store_module = importlib.import_module("punkrecords.store")
usage_module = importlib.import_module("punkrecords.models")

main = cli_module.main
AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
AccountUsage = usage_module.AccountUsage
get_provider = providers_module.get_provider
ProviderCapabilityProfile = providers_module.ProviderCapabilityProfile
ProviderRoutingDecision = providers_module.ProviderRoutingDecision


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


def test_help_and_parser_expose_only_server_cli(capsys):
    assert main(["help"]) == 0
    help_output = capsys.readouterr().out
    assert "uv run punkrecords proxy --host 0.0.0.0 --port 4141" in help_output
    assert "- proxy [--host HOST] [--port PORT]" in help_output
    assert "- help" in help_output
    assert "login" not in help_output
    assert "status" not in help_output
    assert "list" not in help_output
    assert "switch" not in help_output
    assert "tui" not in help_output

    parser = cli_module.build_parser()
    for removed in (["tui"], ["login"], ["status"], ["list"], ["switch", "1"]):
        try:
            parser.parse_args(removed)
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"Expected parser to reject removed command: {removed}")

    args = parser.parse_args(["proxy", "--host", "0.0.0.0", "--port", "4242"])
    assert args.command == "proxy"
    assert args.host == "0.0.0.0"
    assert args.port == 4242


def test_model_exposes_credential_aliases():
    account = make_account("acct-1", "work", "work@example.com")

    assert account.credential_id == "acct-1"
    assert account.credential_label == "work"
    assert account.credential_contact == "work@example.com"

    account.credential_id = "acct-2"
    account.credential_label = "backup"
    account.credential_contact = "backup@example.com"
    account.credential_kind = "api-key"

    assert account.account_id == "acct-2"
    assert account.label == "backup"
    assert account.email == "backup@example.com"
    assert account.auth_mode == "api-key"


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


def test_repository_lists_credentials_per_provider(tmp_path):
    repo = AccountRepository(tmp_path / "accounts.json")
    first = make_account("acct-1", "one", "one@example.com")
    second = make_account("acct-2", "two", "two@example.com")
    third = make_account("acct-3", "three", "three@example.com")
    third.provider = "other-provider"

    repo.upsert_account(first, make_active=True)
    repo.upsert_account(second, make_active=False)
    repo.upsert_account(third, make_active=False)

    credentials = repo.list_provider_credentials("openai-codex")
    assert [credential.account_id for credential in credentials] == ["acct-1", "acct-2"]


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

        def classify_routing_failure(self, status_code, body):
            return ProviderRoutingDecision(status_code >= 500, "fake-auth")

        def capability_profile(self):
            return ProviderCapabilityProfile(model_ids=("fake-model",), supports_streaming=False, supports_tools=False, supports_embeddings=False)

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

        def classify_routing_failure(self, status_code, body):
            return ProviderRoutingDecision(status_code >= 500, "fake-proxy")

        def capability_profile(self):
            return ProviderCapabilityProfile(model_ids=("fake-model",), supports_streaming=False, supports_tools=False, supports_embeddings=False)

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


def test_settings_validate_routing_payload(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_PROVIDER_MODULES", raising=False)

    settings_store_module.validate_settings_payload(
        {
            "routing": {
                "provider_order": ["openai-codex"],
                "route_overrides": {"/v1/responses": ["openai-codex"]},
                "model_overrides": {"gpt-5.4": ["openai-codex"]},
            }
        }
    )

    try:
        settings_store_module.validate_settings_payload({"routing": {"provider_order": ["unknown-provider"]}})
    except ValueError as exc:
        assert str(exc) == "Unknown provider in settings.routing.provider_order: unknown-provider"
    else:
        raise AssertionError("Expected unknown provider validation failure")


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


def test_app_home_prefers_new_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "punk-home"))

    assert paths_module.app_home() == tmp_path / "punk-home"


def test_app_home_uses_current_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"


def test_app_home_defaults_to_repo_local_directory(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"
