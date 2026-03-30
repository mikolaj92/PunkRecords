from __future__ import annotations

from typing import Any

from .providers import get_provider, list_providers, require_proxy_provider
from .settings_store import load_settings


def requested_model(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if not isinstance(model, str):
        return None
    value = model.strip()
    return value or None


def ordered_provider_ids(local_path: str, payloads_by_provider: dict[str, Any]) -> list[str]:
    candidate_ids = [
        provider_id
        for provider_id, payload in payloads_by_provider.items()
        if provider_supports_request(provider_id, local_path, payload)
    ]
    if not candidate_ids:
        return []

    model = next((value for value in (requested_model(payload) for payload in payloads_by_provider.values()) if value), None)
    configured = _configured_provider_order(local_path, model)

    ordered: list[str] = []
    for provider_id in configured:
        if provider_id in candidate_ids and provider_id not in ordered:
            ordered.append(provider_id)
    for provider_id in candidate_ids:
        if provider_id not in ordered:
            ordered.append(provider_id)
    return ordered


def should_fallback_to_next_provider(provider_id: str, status_code: int, body: bytes) -> bool:
    proxy_provider = require_proxy_provider(get_provider(provider_id))
    return proxy_provider.classify_routing_failure(status_code, body).allow_fallback


def provider_supports_request(provider_id: str, local_path: str, payload: Any) -> bool:
    proxy_provider = require_proxy_provider(get_provider(provider_id))
    profile = proxy_provider.capability_profile()
    if not proxy_provider.matches_request(local_path, payload):
        return False
    if proxy_provider.is_streaming_request(payload) and not profile.supports_streaming:
        return False
    if local_path == "/v1/embeddings" and not profile.supports_embeddings:
        return False
    if isinstance(payload, dict) and payload.get("tools") and not profile.supports_tools:
        return False

    model = requested_model(payload)
    if model and profile.model_ids and model not in set(profile.model_ids):
        return False
    return True


def _configured_provider_order(local_path: str, model: str | None) -> list[str]:
    routing = load_settings().get("routing", {})
    if not isinstance(routing, dict):
        return [provider.provider_id for provider in list_providers() if provider.proxy is not None]

    model_overrides = routing.get("model_overrides", {})
    if model and isinstance(model_overrides, dict):
        configured = model_overrides.get(model)
        if isinstance(configured, list):
            return _normalized_provider_ids(configured)

    route_overrides = routing.get("route_overrides", {})
    if isinstance(route_overrides, dict):
        configured = route_overrides.get(local_path)
        if isinstance(configured, list):
            return _normalized_provider_ids(configured)

    provider_order = routing.get("provider_order")
    if isinstance(provider_order, list):
        return _normalized_provider_ids(provider_order)
    return [provider.provider_id for provider in list_providers() if provider.proxy is not None]


def _normalized_provider_ids(raw_ids: list[Any]) -> list[str]:
    ordered: list[str] = []
    known_ids = {provider.provider_id for provider in list_providers() if provider.proxy is not None}
    for item in raw_ids:
        if not isinstance(item, str):
            continue
        provider_id = item.strip()
        if not provider_id or provider_id not in known_ids or provider_id in ordered:
            continue
        ordered.append(provider_id)
    return ordered
