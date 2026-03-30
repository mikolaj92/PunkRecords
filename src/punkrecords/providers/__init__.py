from __future__ import annotations

import importlib
import os
import pkgutil
from typing import cast

from punkrecords.models import AccountRecord
from punkrecords.providers.contracts import AuthProvider, BrowserLoginChallenge, DeviceLoginChallenge, LocalRouteSpec, LoginResult, OAuthError, ProviderCapabilityProfile, ProviderDescriptor, ProviderPlugin, ProviderRoutingDecision, ProxyProvider, ProxyRequestSpec, UsageProvider

def _load_builtin_providers() -> tuple[ProviderDescriptor, ...]:
    providers: list[ProviderDescriptor] = []
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name == "contracts":
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        provider = getattr(module, "PROVIDER", None)
        if isinstance(provider, ProviderDescriptor):
            providers.append(provider)
    providers.sort(key=lambda item: item.provider_id)
    return tuple(providers)


def _load_external_providers() -> tuple[ProviderDescriptor, ...]:
    modules = [item.strip() for item in os.getenv("PUNKRECORDS_PROVIDER_MODULES", "").split(",") if item.strip()]
    providers: list[ProviderDescriptor] = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        provider = getattr(module, "PROVIDER", None)
        if isinstance(provider, ProviderDescriptor):
            providers.append(provider)
    providers.sort(key=lambda item: item.provider_id)
    return tuple(providers)


_BUILTIN_PROVIDERS: tuple[ProviderDescriptor, ...] = _load_builtin_providers()
_EXTERNAL_PROVIDERS: tuple[ProviderDescriptor, ...] = _load_external_providers()
_ALL_PROVIDERS: tuple[ProviderDescriptor, ...] = _BUILTIN_PROVIDERS + _EXTERNAL_PROVIDERS
_PROVIDER_IDS = [provider.provider_id for provider in _ALL_PROVIDERS]
if len(_PROVIDER_IDS) != len(set(_PROVIDER_IDS)):
    raise ValueError("Duplicate provider_id values detected in provider registry")
_PROVIDER_REGISTRY = {provider.provider_id: provider for provider in _ALL_PROVIDERS}


def normalize_provider_id(provider_id: str | None) -> str:
    value = str(provider_id or "").strip()
    if value:
        return value
    configured = str(os.getenv("PUNKRECORDS_DEFAULT_PROVIDER", "")).strip()
    if configured:
        return configured
    if len(_ALL_PROVIDERS) == 1:
        return _ALL_PROVIDERS[0].provider_id
    raise KeyError("Provider id is required when multiple providers are available")


def get_provider(provider_id: str | None) -> ProviderDescriptor:
    normalized = normalize_provider_id(provider_id)
    try:
        return _PROVIDER_REGISTRY[normalized]
    except KeyError as exc:
        raise KeyError(f"Unknown provider: {normalized}") from exc


def get_account_provider(account: AccountRecord) -> ProviderDescriptor:
    return get_provider(account.provider)


def require_auth_provider(provider: ProviderDescriptor | AuthProvider) -> AuthProvider:
    auth = getattr(provider, "auth", None)
    if auth is not None:
        return auth
    provider_id = getattr(provider, "provider_id", "unknown")
    raise TypeError(f"Provider {provider_id} does not implement auth capability")


def require_usage_provider(provider: ProviderDescriptor | UsageProvider) -> UsageProvider:
    usage = getattr(provider, "usage", None)
    if usage is not None:
        return usage
    provider_id = getattr(provider, "provider_id", "unknown")
    raise TypeError(f"Provider {provider_id} does not implement usage capability")


def require_proxy_provider(provider: ProviderDescriptor | ProxyProvider) -> ProxyProvider:
    proxy = getattr(provider, "proxy", None)
    if proxy is not None:
        return proxy
    provider_id = getattr(provider, "provider_id", "unknown")
    raise TypeError(f"Provider {provider_id} does not implement proxy capability")


def list_providers() -> list[ProviderDescriptor]:
    return list(_ALL_PROVIDERS)


def supported_provider_metadata() -> list[dict[str, str]]:
    return [{"id": provider.provider_id, "label": provider.label} for provider in _ALL_PROVIDERS]


def providers_for_local_route(path: str, method: str) -> list[ProviderDescriptor]:
    normalized_method = method.upper()
    matches: list[ProviderDescriptor] = []
    for provider in _ALL_PROVIDERS:
        if provider.proxy is None:
            continue
        for route in provider.proxy.local_routes():
            if route.path == path and route.method.upper() == normalized_method:
                matches.append(provider)
                break
    return matches


def all_local_routes() -> list[LocalRouteSpec]:
    routes: dict[tuple[str, str], LocalRouteSpec] = {}
    for provider in _ALL_PROVIDERS:
        if provider.proxy is None:
            continue
        for route in provider.proxy.local_routes():
            routes[(route.path, route.method.upper())] = LocalRouteSpec(path=route.path, method=route.method.upper())
    return sorted(routes.values(), key=lambda route: (route.path, route.method))


__all__ = [
    "BrowserLoginChallenge",
    "DeviceLoginChallenge",
    "LoginResult",
    "OAuthError",
    "ProviderCapabilityProfile",
    "ProviderDescriptor",
    "ProviderPlugin",
    "ProviderRoutingDecision",
    "ProxyRequestSpec",
    "all_local_routes",
    "get_account_provider",
    "get_provider",
    "list_providers",
    "normalize_provider_id",
    "providers_for_local_route",
    "require_auth_provider",
    "require_proxy_provider",
    "require_usage_provider",
    "supported_provider_metadata",
]
