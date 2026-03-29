from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .paths import settings_path
from .store import atomic_write_json


def default_settings() -> dict[str, Any]:
    return {
        "proxy": {
            "host": "127.0.0.1",
            "port": 4141,
            "max_attempts": 3,
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

    allowed_root = {"proxy"}
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
        elif isinstance(value, dict):
            _validate_settings_patch(value, path=current_path)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
