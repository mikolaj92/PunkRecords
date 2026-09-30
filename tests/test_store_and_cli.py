from __future__ import annotations

import base64
import importlib
import json
import time
from dataclasses import dataclass
from typing import Mapping, cast

import pytest

from punkrecords.providers.contracts import AuthProvider

cli_module = importlib.import_module("punkrecords.cli")
models_module = importlib.import_module("punkrecords.models")
oauth_module = importlib.import_module("punkrecords.oauth")
openai_codex_module = importlib.import_module("punkrecords.providers.openai_codex")
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


def make_account(
    account_id: str, label: str, email: str, *, provider: str = "openai-codex"
) -> AccountRecord:
    return AccountRecord(
        id=f"local-{account_id}",
        external_id=account_id,
        contact=email,
        display_name=label,
        provider=provider,
        created_at="2026-03-27T00:00:00Z",
        last_refresh="2026-03-27T00:00:00Z",
        last_used="2026-03-27T00:00:00Z",
        provider_state={
            "tokens": {
                "access_token": _access_token(account_id, email),
                "refresh_token": f"refresh-{account_id}",
                "account_id": account_id,
            }
        },
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
    assert "future web UI" not in help_output
    assert "GET /" in help_output or "dashboard" in help_output.lower()

    parser = cli_module.build_parser()
    for removed in (["tui"], ["login"], ["status"], ["list"], ["switch", "1"]):
        try:
            parser.parse_args(removed)
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(
                f"Expected parser to reject removed command: {removed}"
            )

    args = parser.parse_args(["proxy", "--host", "0.0.0.0", "--port", "4242"])
    assert args.command == "proxy"
    assert args.host == "0.0.0.0"
    assert args.port == 4242


def test_model_exposes_one_canonical_account_shape():
    account = make_account("acct-1", "work", "work@example.com")
    payload = account.to_dict()
    assert payload["external_id"] == "acct-1"
    assert payload["display_name"] == "work"
    assert payload["contact"] == "work@example.com"
    for legacy in ("account_id", "email", "label", "auth_mode", "source", "tokens"):
        assert legacy not in payload
    assert payload["provider_state"]["tokens"]["account_id"] == "acct-1"


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"external_id": "acct-1", "display_name": "work"},
        {"external_id": "acct-1", "display_name": ""},
        {"external_id": "", "display_name": "work"},
        {"external_id": "", "display_name": ""},
    ],
)
def test_usage_exposes_one_canonical_shape(identity):
    usage = AccountUsage(
        **identity,
        provider="openai-codex",
        details={"plan_type": "plus"},
    )
    payload = usage.to_dict()
    empty_window = {
        "used_percent": None,
        "limit_window_seconds": None,
        "reset_after_seconds": None,
        "reset_at": None,
    }
    assert payload == {
        "external_id": identity.get("external_id", ""),
        "display_name": identity.get("display_name", ""),
        "provider": "openai-codex",
        "details": {"plan_type": "plus"},
        "error": None,
        "plan_type": "plus",
        "primary_window": empty_window,
        "secondary_window": empty_window,
    }
    serialized = json.loads(json.dumps(payload))
    assert serialized == payload
    for key in ("account_id", "label"):
        assert key not in serialized
    for legacy in ("account_id", "label"):
        assert legacy not in usage.__dict__
        assert not hasattr(usage, legacy)
        assert not isinstance(getattr(type(usage), legacy, None), property)

    usage.external_id = "acct-2"
    usage.display_name = "personal"
    updated_payload = usage.to_dict()
    assert updated_payload["external_id"] == "acct-2"
    assert updated_payload["display_name"] == "personal"
    assert set(updated_payload) == set(payload)


@pytest.mark.parametrize("legacy", ["account_id", "label"])
@pytest.mark.parametrize(
    "canonical", [{}, {"external_id": "acct-1", "display_name": "work"}]
)
def test_usage_rejects_legacy_identity_arguments(legacy, canonical):
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{legacy}'"):
        AccountUsage(**canonical, **{legacy: "legacy-value"})


def test_usage_preserves_plan_windows_details_and_errors():
    primary = models_module.UsageWindow(12.5, 18000, 120, 1900000000)
    secondary = models_module.UsageWindow(67.5, 604800, 240, 1900000120)
    details = {"provider_extra": "retained"}
    usage = AccountUsage(
        external_id="acct-1",
        display_name="work",
        provider="openai-codex",
        details=details,
        plan_type="plus",
        primary_window=primary,
        secondary_window=secondary,
    )
    payload = usage.to_dict()
    assert details == {"provider_extra": "retained"}
    assert payload["details"] == {
        "provider_extra": "retained",
        "plan_type": "plus",
        "primary_window": primary.to_dict(),
        "secondary_window": secondary.to_dict(),
    }
    assert usage.plan_type == payload["plan_type"] == "plus"
    assert (
        usage.primary_window.to_dict() == payload["primary_window"] == primary.to_dict()
    )
    assert (
        usage.secondary_window.to_dict()
        == payload["secondary_window"]
        == secondary.to_dict()
    )
    assert payload["error"] is None
    assert json.loads(json.dumps(payload)) == payload
    assert "account_id" not in payload
    assert "label" not in payload
    report = openai_codex_module.build_codex_usage_report([usage])
    assert report["rows"][0][:4] == ["work", "openai-codex", "plus", "12.5%"]
    assert report["rows"][0][5] == "67.5%"

    usage.error = "quota unavailable"
    assert json.loads(json.dumps(usage.to_dict())) == {
        **payload,
        "error": "quota unavailable",
    }
    report = openai_codex_module.build_codex_usage_report([usage])
    assert report["summary"]["failed_accounts"] == ["work"]
    assert report["rows"][0][3:] == [
        "error",
        "quota unavailable",
        "error",
        "quota unavailable",
    ]


def test_repository_load_rejects_missing_provider(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_account_id": "local-acct-missing-provider",
                "accounts": [
                    {
                        "id": "local-acct-missing-provider",
                        "account_id": "acct-missing-provider",
                        "email": "missing@example.com",
                        "label": "missing-provider",
                        "tokens": {
                            "access_token": "access",
                            "refresh_token": "refresh",
                            "account_id": "acct-missing-provider",
                        },
                    }
                ],
            }
        )
    )

    repo = AccountRepository(path)
    try:
        repo.load()
    except ValueError as exc:
        assert "missing required field 'provider'" in str(exc)
        assert "local-acct-missing-provider" in str(exc)
    else:
        raise AssertionError(
            "Expected load to fail closed for accounts without provider"
        )


def test_repository_upsert_rejects_missing_provider(tmp_path):
    repo = AccountRepository(tmp_path / "accounts.json")
    account = make_account("acct-1", "work", "work@example.com", provider="")

    try:
        repo.upsert_account(account, make_active=True)
    except ValueError as exc:
        assert "missing required field 'provider'" in str(exc)
    else:
        raise AssertionError(
            "Expected upsert to fail closed for accounts without provider"
        )


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
    assert [credential.external_id for credential in credentials] == [
        "acct-1",
        "acct-2",
    ]


def test_provider_registry_exposes_builtin_openai_codex():
    provider = get_provider(None)

    assert provider.provider_id == "openai-codex"
    assert provider.label == "OpenAI Codex"
    assert providers_module.supported_provider_metadata() == [
        {"id": "openai-codex", "label": "OpenAI Codex"}
    ]


def test_openai_codex_proxy_upstream_url_uses_modern_overrides_only(monkeypatch):
    openai_codex = importlib.import_module("punkrecords.providers.openai_codex")

    monkeypatch.delenv("PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", raising=False)
    monkeypatch.delenv(
        "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_V1_RESPONSES_URL", raising=False
    )
    monkeypatch.setenv(
        "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL", "http://legacy.example/responses"
    )

    assert (
        openai_codex.proxy_upstream_url("/v1/responses")
        == "https://chatgpt.com/backend-api/codex/responses"
    )

    monkeypatch.setenv(
        "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_V1_RESPONSES_URL",
        "http://modern.example/custom-responses",
    )
    assert (
        openai_codex.proxy_upstream_url("/v1/responses")
        == "http://modern.example/custom-responses"
    )

    monkeypatch.delenv(
        "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_V1_RESPONSES_URL", raising=False
    )
    monkeypatch.setenv(
        "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE", "http://base.example/codex"
    )
    assert (
        openai_codex.proxy_upstream_url("/v1/responses")
        == "http://base.example/codex/responses"
    )
    assert (
        openai_codex.proxy_upstream_url("/v1/embeddings")
        == "http://base.example/codex/embeddings"
    )
    assert (
        openai_codex.proxy_upstream_override_env_key("/v1/responses")
        == "PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_V1_RESPONSES_URL"
    )


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
            return account, AccountUsage(
                external_id=account.external_id,
                display_name=account.display_name,
                provider=account.provider,
                plan_type="fake",
            )

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
            return (
                local_path == "/v1/fake" and payload.get("provider") == self.provider_id
            )

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            return providers_module.ProxyRequestSpec(
                url=self.proxy_upstream_url(local_path),
                data=b"{}",
                headers={"X-Test": idempotency_key},
                method="POST",
            )

        def proxy_headers(self, account, *, stream, idempotency_key):
            return {"X-Test": idempotency_key}

        def proxy_extract_usage(self, payload, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def create_stream_usage_tracker(self, local_path):
            class Tracker:
                usage = {
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                }

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
            return ProviderCapabilityProfile(
                model_ids=("fake-model",),
                supports_streaming=False,
                supports_tools=False,
                supports_embeddings=False,
            )

        def build_provider_state(self, account):
            return {"fake_state": account.provider_state}

        def extract_provider_identity(self, payload):
            return None

        def start_browser_login(self, *, label=None, redirect_uri=None):
            raise NotImplementedError

        def wait_browser_login_callback(self, state, timeout=300.0):
            raise NotImplementedError

        def complete_browser_login(self, challenge, authorization_code):
            raise NotImplementedError

    @dataclass
    class FakeUsage:
        provider_id: str = "fake-external"

        def fetch_account_usage(self, account, timeout=15.0):
            return account, AccountUsage(
                external_id=account.external_id,
                display_name=account.display_name,
                provider=account.provider,
                plan_type="fake",
            )

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
            return (
                local_path == "/v1/fake" and payload.get("provider") == self.provider_id
            )

        def proxy_upstream_url(self, local_path):
            return "https://example.invalid/fake"

        def build_proxy_request(self, account, *, local_path, payload, idempotency_key):
            return providers_module.ProxyRequestSpec(
                url=self.proxy_upstream_url(local_path),
                data=b"{}",
                headers={"X-Test": idempotency_key},
                method="POST",
            )

        def proxy_headers(self, account, *, stream, idempotency_key):
            return {"X-Test": idempotency_key}

        def proxy_extract_usage(self, payload, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def proxy_extract_usage_from_body(self, body, local_path):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

        def create_stream_usage_tracker(self, local_path):
            class Tracker:
                usage = {
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                }

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
            return ProviderCapabilityProfile(
                model_ids=("fake-model",),
                supports_streaming=False,
                supports_tools=False,
                supports_embeddings=False,
            )

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
        assert any(
            item["id"] == "fake-external"
            for item in reloaded.supported_provider_metadata()
        )
        record = make_account("acct-fake", "fake", "fake@example.com")
        record.provider = "fake-external"
        record.provider_state = {
            "fake": "initial",
            "tokens": {"access_token": "", "refresh_token": "", "account_id": ""},
        }
        refreshed = reloaded.require_auth_provider(provider).maybe_refresh_account(
            record
        )
        assert refreshed.provider_state["fake"] == "refreshed"
        assert (
            reloaded.require_proxy_provider(provider).local_routes()[0].path
            == "/v1/fake"
        )
        assert (
            reloaded.require_proxy_provider(provider).list_models()["data"][0]["id"]
            == "fake-model"
        )
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
        settings_store_module.validate_settings_payload(
            {"routing": {"provider_order": ["unknown-provider"]}}
        )
    except ValueError as exc:
        assert (
            str(exc)
            == "Unknown provider in settings.routing.provider_order: unknown-provider"
        )
    else:
        raise AssertionError("Expected unknown provider validation failure")


def test_repository_treats_same_account_id_from_different_providers_as_distinct(
    tmp_path,
):
    repo = AccountRepository(tmp_path / "accounts.json")
    first = make_account("acct-shared", "one", "one@example.com")
    second = make_account("acct-shared", "two", "two@example.com")
    second.provider = "other-provider"

    repo.upsert_account(first, make_active=True)
    repo.upsert_account(second, make_active=False)

    accounts = repo.list_accounts()
    assert len(accounts) == 2
    assert {account.provider for account in accounts} == {
        "openai-codex",
        "other-provider",
    }


def test_app_home_prefers_new_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "punk-home"))

    assert paths_module.app_home() == tmp_path / "punk-home"


def test_app_home_uses_current_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"


def test_app_home_defaults_to_repo_local_directory(monkeypatch):
    monkeypatch.delenv("PUNKRECORDS_HOME", raising=False)

    assert paths_module.app_home() == paths_module.project_root() / ".punkrecords"


def test_browser_callback_uses_challenge_provider(monkeypatch):
    calls = []

    class FakeAuth:
        def login_via_browser_flow(self, *, label=None):
            raise NotImplementedError

        def login_via_device_flow(self, *, label=None, headless=False):
            raise NotImplementedError

        def start_device_login(self, *, label=None):
            raise NotImplementedError

        def start_browser_login(self, *, label=None, redirect_uri=None):
            raise NotImplementedError

        def wait_browser_login_callback(self, state, timeout=300.0):
            calls.append((state, timeout))
            return "provider-code"

        def complete_browser_login(self, challenge, authorization_code):
            raise NotImplementedError

        def poll_device_login(self, challenge):
            raise NotImplementedError

        def maybe_refresh_account(self, account):
            return account

    descriptor = providers_module.ProviderDescriptor(
        provider_id="challenge-provider",
        label="Challenge Provider",
        auth=FakeAuth(),
    )
    monkeypatch.setattr(
        oauth_module,
        "get_provider",
        lambda provider_id=None: (
            descriptor if provider_id == "challenge-provider" else None
        ),
    )
    challenge = providers_module.BrowserLoginChallenge(
        provider_id="challenge-provider",
        authorize_url="https://example.invalid",
        redirect_uri="http://localhost/callback",
        code_verifier="verifier",
        state="challenge-state",
        issuer="https://issuer.invalid",
        token_url="https://issuer.invalid/token",
        client_id="client",
    )
    assert (
        oauth_module.wait_browser_login_callback(challenge, timeout=12.0)
        == "provider-code"
    )
    assert calls == [("challenge-state", 12.0)]


def test_require_auth_provider_rejects_partial_capability():
    class PartialAuth:
        def wait_browser_login_callback(self, state, timeout=300.0):
            return "code"

    descriptor = providers_module.ProviderDescriptor(
        provider_id="partial", label="Partial", auth=cast(AuthProvider, PartialAuth())
    )
    with pytest.raises(TypeError, match="complete auth capability"):
        providers_module.require_auth_provider(descriptor)


def test_codex_refresh_updates_external_id_from_canonical_tokens(monkeypatch):
    account = make_account("old-account", "work", "work@example.com")
    account.provider_state["tokens"] = {
        "access_token": "old-token",
        "refresh_token": "refresh-token",
        "account_id": "old-account",
    }
    provider = providers_module.require_auth_provider(
        providers_module.get_provider("openai-codex")
    )
    monkeypatch.setattr(
        openai_codex_module, "access_token_expiring", lambda token: True
    )
    monkeypatch.setattr(
        provider,
        "refresh_tokens",
        lambda tokens: AccountTokens("new-token", "new-refresh", "new-account"),
    )

    refreshed = provider.maybe_refresh_account(account)

    assert refreshed.external_id == "new-account"
    assert refreshed.provider_state["tokens"] == {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "account_id": "new-account",
    }


def test_codex_rejects_missing_canonical_token_slot():
    account = make_account("acct", "work", "work@example.com")
    account.provider_state = {"tokens": "not-an-object"}
    provider = providers_module.require_auth_provider(
        providers_module.get_provider("openai-codex")
    )
    with pytest.raises(providers_module.OAuthError, match="canonical tokens object"):
        provider.maybe_refresh_account(account)
