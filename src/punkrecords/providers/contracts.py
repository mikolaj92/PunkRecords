from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from punkrecords.models import AccountRecord, AccountUsage


class OAuthError(RuntimeError):
    pass


@dataclass
class LoginResult:
    account: AccountRecord
    base_url: str


@dataclass
class DeviceLoginChallenge:
    provider_id: str
    device_auth_id: str
    user_code: str
    verification_url: str
    poll_interval: int
    issuer: str
    token_url: str
    client_id: str
    label: str | None = None


@dataclass
class BrowserLoginChallenge:
    provider_id: str
    authorize_url: str
    redirect_uri: str
    code_verifier: str
    state: str
    issuer: str
    token_url: str
    client_id: str
    label: str | None = None


UsageSummary = dict[str, int | None]


class StreamUsageTracker(Protocol):
    usage: UsageSummary

    def feed(self, chunk: bytes) -> None: ...


@dataclass
class ProxyRequestSpec:
    url: str
    data: bytes
    headers: dict[str, str]
    method: str = "POST"


@dataclass(frozen=True)
class LocalRouteSpec:
    path: str
    method: str = "POST"


@dataclass(frozen=True)
class ProviderCapabilityProfile:
    model_ids: tuple[str, ...] = ()
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_embeddings: bool = False


@dataclass(frozen=True)
class ProviderRoutingDecision:
    allow_fallback: bool
    reason: str = ""


class ProviderPlugin(Protocol):
    provider_id: str
    label: str


class AuthProvider(Protocol):
    def login_via_browser_flow(self, *, label: str | None = None) -> LoginResult: ...

    def login_via_device_flow(self, *, label: str | None = None, headless: bool = False) -> LoginResult: ...

    def start_device_login(self, *, label: str | None = None) -> DeviceLoginChallenge: ...

    def poll_device_login(self, challenge: DeviceLoginChallenge) -> LoginResult | None: ...

    def maybe_refresh_account(self, account: AccountRecord) -> AccountRecord: ...


class UsageProvider(Protocol):
    def fetch_account_usage(self, account: AccountRecord, timeout: float = 15.0) -> tuple[AccountRecord, AccountUsage]: ...

    def usage_url(self) -> str: ...

    def build_usage_report(self, usages: list[AccountUsage]) -> dict[str, Any]: ...

    def format_usage_table(self, usages: list[AccountUsage]) -> list[str]: ...


class ProxyProvider(Protocol):
    def local_paths(self) -> tuple[str, ...]: ...

    def local_routes(self) -> tuple[LocalRouteSpec, ...]: ...

    def parse_local_request(self, *, local_path: str, method: str, raw_body: bytes, headers: dict[str, str]) -> Any: ...

    def is_streaming_request(self, payload: Any) -> bool: ...

    def matches_request(self, local_path: str, payload: Any) -> bool: ...

    def proxy_upstream_url(self, local_path: str) -> str: ...

    def build_proxy_request(self, account: AccountRecord, *, local_path: str, payload: Any, idempotency_key: str) -> ProxyRequestSpec: ...

    def proxy_headers(self, account: AccountRecord, *, stream: bool, idempotency_key: str) -> dict[str, str]: ...

    def proxy_extract_usage(self, payload: dict[str, Any], local_path: str) -> UsageSummary: ...

    def proxy_extract_usage_from_body(self, body: bytes, local_path: str) -> UsageSummary: ...

    def create_stream_usage_tracker(self, local_path: str) -> StreamUsageTracker: ...

    def list_models(self) -> dict[str, Any]: ...

    def classify_proxy_failure(self, status_code: int, body: bytes) -> tuple[bool, int]: ...

    def classify_routing_failure(self, status_code: int, body: bytes) -> ProviderRoutingDecision: ...

    def capability_profile(self) -> ProviderCapabilityProfile: ...

    def describe_local_routes(self, *, base_url: str) -> list[tuple[str, str]]: ...


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    label: str
    auth: AuthProvider | None = None
    usage: UsageProvider | None = None
    proxy: ProxyProvider | None = None
