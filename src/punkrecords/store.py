from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import AccountRecord, StateSnapshot
from .paths import accounts_path

_LEGACY_PROVIDER_ID = "openai-codex"


def _migrate_legacy_account_provider(account: AccountRecord) -> AccountRecord:
    if not account.provider:
        account.provider = _LEGACY_PROVIDER_ID
    return account


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    temp_path.replace(path)


class AccountRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or accounts_path()

    def load(self) -> StateSnapshot:
        if not self.path.exists():
            return StateSnapshot()
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError("State file must contain a JSON object")
        state = StateSnapshot.from_dict(data)
        state.accounts = [_migrate_legacy_account_provider(account) for account in state.accounts]
        return state

    def save(self, state: StateSnapshot) -> None:
        atomic_write_json(self.path, state.to_dict())

    def list_accounts(self) -> list[AccountRecord]:
        return self.load().accounts

    @staticmethod
    def _same_account(existing: AccountRecord, candidate: AccountRecord) -> bool:
        if existing.id and candidate.id and existing.id == candidate.id and existing.provider == candidate.provider:
            return True
        return existing.provider == candidate.provider and existing.external_id == candidate.external_id

    def get_active(self) -> AccountRecord | None:
        state = self.load()
        if not state.active_account_id:
            return None
        return next((account for account in state.accounts if account.id == state.active_account_id), None)

    def resolve_account(self, ident: str) -> AccountRecord:
        state = self.load()
        if ident.isdigit():
            index = int(ident) - 1
            if 0 <= index < len(state.accounts):
                return state.accounts[index]
        provider_hint = None
        raw_ident = ident
        if ":" in ident:
            possible_provider, possible_ident = ident.split(":", 1)
            if possible_provider and possible_ident:
                provider_hint = possible_provider
                raw_ident = possible_ident
        matches = [
            account
            for account in state.accounts
            if raw_ident in {account.id, account.external_id, account.display_name}
            and (provider_hint is None or account.provider == provider_hint)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError(f"Ambiguous account identifier: {ident}. Use provider:id, provider:label, or the internal id.")
        raise KeyError(f"Unknown account: {ident}")

    def upsert_account(self, account: AccountRecord, *, make_active: bool = True) -> AccountRecord:
        state = self.load()
        replacement_index = None
        for index, existing in enumerate(state.accounts):
            if self._same_account(existing, account):
                replacement_index = index
                break

        account.last_used = utc_now_iso()
        if replacement_index is None:
            if not account.created_at:
                account.created_at = utc_now_iso()
            state.accounts.append(account)
        else:
            original = state.accounts[replacement_index]
            if not account.created_at:
                account.created_at = original.created_at or utc_now_iso()
            state.accounts[replacement_index] = account

        if make_active:
            state.active_account_id = account.id
        self.save(state)
        return account

    def replace_account(self, account: AccountRecord, *, make_active: bool | None = None) -> AccountRecord:
        state = self.load()
        replacement_index = None
        for index, existing in enumerate(state.accounts):
            if self._same_account(existing, account):
                replacement_index = index
                break

        if replacement_index is None:
            state.accounts.append(account)
        else:
            original = state.accounts[replacement_index]
            if not account.created_at:
                account.created_at = original.created_at
            state.accounts[replacement_index] = account

        if make_active is True:
            state.active_account_id = account.id
        elif make_active is False and state.active_account_id == account.id:
            state.active_account_id = None
        self.save(state)
        return account

    def set_active(self, ident: str) -> AccountRecord:
        state = self.load()
        account = self.resolve_account(ident)
        state.active_account_id = account.id
        for existing in state.accounts:
            if existing.id == account.id:
                existing.last_used = utc_now_iso()
        self.save(state)
        return account

    def list_enabled_accounts(self) -> list[AccountRecord]:
        return [account for account in self.load().accounts if account.enabled]

    def list_proxy_candidates(self, provider_id: str | None = None) -> list[AccountRecord]:
        state = self.load()
        active_id = state.active_account_id
        now = datetime.now(timezone.utc)

        def _cooled_down(account: AccountRecord) -> bool:
            if not account.cooldown_until:
                return False
            try:
                value = datetime.fromisoformat(account.cooldown_until.replace("Z", "+00:00"))
            except ValueError:
                return False
            return value > now

        enabled = [account for account in state.accounts if account.enabled and not _cooled_down(account)]
        if provider_id:
            enabled = [account for account in enabled if account.provider == provider_id]
        active = [account for account in enabled if account.id == active_id]
        others = [account for account in enabled if account.id != active_id]
        return active + others

    def mark_proxy_failure(self, account_id: str, *, provider_id: str | None = None, error: str, cooldown_seconds: int = 60) -> None:
        state = self.load()
        cooldown_until = datetime.now(timezone.utc).timestamp() + max(0, cooldown_seconds)
        cooldown_iso = datetime.fromtimestamp(cooldown_until, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        now_iso = utc_now_iso()
        for account in state.accounts:
            if account.external_id == account_id and (provider_id is None or account.provider == provider_id):
                account.last_proxy_error = error
                account.last_proxy_error_at = now_iso
                account.cooldown_until = cooldown_iso
                break
        self.save(state)

    def mark_proxy_success(self, account_id: str, *, provider_id: str | None = None) -> None:
        state = self.load()
        for account in state.accounts:
            if account.external_id == account_id and (provider_id is None or account.provider == provider_id):
                account.last_proxy_error = None
                account.last_proxy_error_at = None
                account.cooldown_until = None
                account.last_used = utc_now_iso()
                state.active_account_id = account.id
                break
        self.save(state)

    def admin_accounts_snapshot(self) -> list[dict[str, object | None | bool]]:
        state = self.load()
        active_id = state.active_account_id
        now = datetime.now(timezone.utc)

        def _eligible(account: AccountRecord) -> bool:
            if not account.enabled:
                return False
            if not account.cooldown_until:
                return True
            try:
                value = datetime.fromisoformat(account.cooldown_until.replace("Z", "+00:00"))
            except ValueError:
                return True
            return value <= now

        return [
            {
                "id": account.id,
                "account_id": account.external_id,
                "email": account.contact,
                "label": account.display_name,
                "provider": account.provider,
                "active": account.id == active_id,
                "enabled": account.enabled,
                "eligible": _eligible(account),
                "cooldown_until": account.cooldown_until,
                "last_proxy_error": account.last_proxy_error,
                "last_proxy_error_at": account.last_proxy_error_at,
                "last_used": account.last_used,
                "last_refresh": account.last_refresh,
            }
            for account in state.accounts
        ]
