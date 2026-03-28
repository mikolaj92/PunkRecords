from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AccountRecord
from .paths import hermes_auth_path
from .store import atomic_write_json


def load_hermes_auth(path: Path | None = None) -> dict[str, Any]:
    target = path or hermes_auth_path()
    if not target.exists():
        return {"version": 1, "providers": {}, "active_provider": "openai-codex"}
    payload = json.loads(target.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Hermes auth.json must contain a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("providers", {})
    return payload


def build_codex_provider_state(account: AccountRecord) -> dict[str, Any]:
    return {
        "tokens": {
            "access_token": account.tokens.access_token,
            "refresh_token": account.tokens.refresh_token,
            "account_id": account.tokens.account_id or account.account_id,
        },
        "last_refresh": account.last_refresh or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "auth_mode": account.auth_mode,
    }


def sync_account_to_hermes(account: AccountRecord, path: Path | None = None) -> Path:
    target = path or hermes_auth_path()
    payload = load_hermes_auth(target)
    providers = payload.setdefault("providers", {})
    providers["openai-codex"] = build_codex_provider_state(account)
    payload["active_provider"] = "openai-codex"
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(target, payload)
    return target


def hermes_synced_account_id(path: Path | None = None) -> str | None:
    payload = load_hermes_auth(path)
    provider_state = (payload.get("providers") or {}).get("openai-codex") or {}
    tokens = provider_state.get("tokens") or {}
    account_id = tokens.get("account_id")
    return str(account_id) if account_id else None
