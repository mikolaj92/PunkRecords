from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .paths import settings_path
from .store import atomic_write_json


def default_settings() -> dict[str, Any]:
    return {
        "proxy": {
            "host": "0.0.0.0",
            "port": 4141,
            "max_attempts": 3,
        },
        "routing": {
            "provider_order": ["openai-codex"],
            "route_overrides": {},
            "model_overrides": {},
        },
    }


def load_settings() -> dict[str, Any]:
    path = settings_path()
    base = default_settings()
    if not path.exists():
        return base
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        return base
    return _merge_dicts(base, payload)


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_dicts(default_settings(), payload)
    atomic_write_json(settings_path(), merged)
    return merged


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    _validate_settings_patch(patch)
    merged = _merge_dicts(load_settings(), patch)
    atomic_write_json(settings_path(), merged)
    return merged


def validate_settings_payload(payload: dict[str, Any]) -> None:
    _validate_settings_patch(payload)


def _validate_settings_patch(payload: dict[str, Any], *, path: str = "settings") -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must be an object")

    allowed_root = {"proxy", "routing"}
    for key, value in payload.items():
        if path == "settings" and key not in allowed_root:
            raise ValueError(f"Unsupported settings key: {key}")

        current_path = f"{path}.{key}" if path else key
        if key == "proxy":
            if not isinstance(value, dict):
                raise ValueError("settings.proxy must be an object")
            allowed_proxy = {"host", "port", "max_attempts"}
            for proxy_key, proxy_value in value.items():
                if proxy_key not in allowed_proxy:
                    raise ValueError(f"Unsupported settings key: proxy.{proxy_key}")
                if proxy_key == "host" and not (isinstance(proxy_value, str) and proxy_value.strip()):
                    raise ValueError("settings.proxy.host must be a non-empty string")
                if proxy_key == "port" and not (isinstance(proxy_value, int) and 1 <= proxy_value <= 65535):
                    raise ValueError("settings.proxy.port must be an integer between 1 and 65535")
                if proxy_key == "max_attempts" and not (isinstance(proxy_value, int) and proxy_value >= 1):
                    raise ValueError("settings.proxy.max_attempts must be an integer >= 1")
        elif key == "routing":
            if not isinstance(value, dict):
                raise ValueError("settings.routing must be an object")
            allowed_routing = {"provider_order", "route_overrides", "model_overrides"}
            for routing_key, routing_value in value.items():
                if routing_key not in allowed_routing:
                    raise ValueError(f"Unsupported settings key: routing.{routing_key}")
                if routing_key == "provider_order":
                    _validate_provider_list(routing_value, path="settings.routing.provider_order")
                elif routing_key in {"route_overrides", "model_overrides"}:
                    if not isinstance(routing_value, dict):
                        raise ValueError(f"settings.routing.{routing_key} must be an object")
                    for override_key, override_value in routing_value.items():
                        if not isinstance(override_key, str) or not override_key.strip():
                            raise ValueError(f"settings.routing.{routing_key} keys must be non-empty strings")
                        _validate_provider_list(override_value, path=f"settings.routing.{routing_key}.{override_key}")
        elif isinstance(value, dict):
            _validate_settings_patch(value, path=current_path)


def _validate_provider_list(value: Any, *, path: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path} must be a non-empty list")
    provider_ids = _known_provider_ids()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path} values must be non-empty strings")
        if item not in provider_ids:
            raise ValueError(f"Unknown provider in {path}: {item}")


def _known_provider_ids() -> set[str]:
    providers_module = __import__("punkrecords.providers", fromlist=["supported_provider_metadata"])
    metadata = providers_module.supported_provider_metadata()
    return {str(item["id"]) for item in metadata if isinstance(item, dict) and isinstance(item.get("id"), str)}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
