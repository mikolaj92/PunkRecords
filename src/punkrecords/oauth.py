from __future__ import annotations

from punkrecords.models import AccountRecord
from punkrecords.providers import DeviceLoginChallenge, LoginResult, OAuthError, get_account_provider, get_provider, require_auth_provider


def start_device_login(*, provider_id: str | None = None, label: str | None = None) -> DeviceLoginChallenge:
    return require_auth_provider(get_provider(provider_id)).start_device_login(label=label)


def poll_device_login(challenge: DeviceLoginChallenge) -> LoginResult | None:
    return require_auth_provider(get_provider(challenge.provider_id)).poll_device_login(challenge)


def maybe_refresh_account(account: AccountRecord) -> AccountRecord:
    return require_auth_provider(get_account_provider(account)).maybe_refresh_account(account)


def login_via_browser_flow(*, provider_id: str | None = None, label: str | None = None) -> LoginResult:
    return require_auth_provider(get_provider(provider_id)).login_via_browser_flow(label=label)


def login_via_device_flow(*, provider_id: str | None = None, label: str | None = None, headless: bool = False) -> LoginResult:
    return require_auth_provider(get_provider(provider_id)).login_via_device_flow(label=label, headless=headless)
