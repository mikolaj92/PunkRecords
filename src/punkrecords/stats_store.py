from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .paths import proxy_requests_path, proxy_rollups_path
from .store import atomic_write_json


def _default_rollups() -> dict[str, Any]:
    return {
        "request_count": 0,
        "success_count": 0,
        "error_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "by_endpoint": {},
        "by_account": {},
        "by_provider_account": {},
        "updated_at": None,
    }


def load_rollups() -> dict[str, Any]:
    path = proxy_rollups_path()
    if not path.exists():
        return _default_rollups()
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else _default_rollups()


def record_request(summary: dict[str, Any]) -> None:
    requests_path = proxy_requests_path()
    requests_path.parent.mkdir(parents=True, exist_ok=True)
    with requests_path.open("a") as handle:
        handle.write(json.dumps(summary, sort_keys=True) + "\n")

    rollups = load_rollups()
    rollups["request_count"] += 1
    if int(summary.get("status_code", 0)) < 400:
        rollups["success_count"] += 1
    else:
        rollups["error_count"] += 1

    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = summary.get(field)
        if isinstance(value, int):
            rollups[field] += value

    endpoint = str(summary.get("endpoint") or "unknown")
    by_endpoint = rollups.setdefault("by_endpoint", {})
    by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1

    account_id = str(summary.get("account_id") or "unknown")
    provider_id = str(summary.get("provider_id") or "unknown")
    provider_account_key = f"{provider_id}:{account_id}"
    by_account = rollups.setdefault("by_account", {})
    by_account[provider_account_key] = by_account.get(provider_account_key, 0) + 1
    by_provider_account = rollups.setdefault("by_provider_account", {})
    by_provider_account[provider_account_key] = by_provider_account.get(provider_account_key, 0) + 1

    rollups["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    atomic_write_json(proxy_rollups_path(), rollups)


def load_request_history(limit: int = 100) -> list[dict[str, Any]]:
    path = proxy_requests_path()
    if not path.exists():
        return []

    lines = path.read_text().splitlines()
    selected = lines[-max(0, limit) :] if limit > 0 else lines
    history: list[dict[str, Any]] = []
    for line in reversed(selected):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            history.append(payload)
    return history
