from __future__ import annotations

from punkrecords.models import AccountRecord, AccountUsage
from punkrecords.providers.contracts import ProviderPlugin
from punkrecords.providers import get_account_provider, get_provider, require_usage_provider


def usage_url(provider_id: str | None = None) -> str:
    provider: ProviderPlugin = require_usage_provider(get_provider(provider_id))
    return provider.usage_url()


def fetch_account_usage(account: AccountRecord, timeout: float = 15.0) -> tuple[AccountRecord, AccountUsage]:
    return require_usage_provider(get_account_provider(account)).fetch_account_usage(account, timeout=timeout)


def fetch_default_provider_usage(account: AccountRecord, timeout: float = 15.0, provider_id: str | None = None) -> tuple[AccountRecord, AccountUsage]:
    return require_usage_provider(get_provider(provider_id)).fetch_account_usage(account, timeout=timeout)
