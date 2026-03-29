from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from typing import Any, Callable

from punkrecords.models import AccountRecord, AccountUsage
from punkrecords.paths import accounts_path, app_home
from punkrecords.providers import OAuthError, get_account_provider, get_provider, list_providers, require_auth_provider, require_usage_provider
from punkrecords.proxy import run_proxy_server
from punkrecords.store import AccountRepository
from punkrecords.tui import select_account_index, select_provider_id, run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="punkrecords", description="Run a self-contained local provider-driven proxy runtime with multi-account OAuth state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="Show practical usage instructions")
    subparsers.add_parser("tui", help="Open arrow-key TUI menu")
    proxy_parser = subparsers.add_parser("proxy", help="Run local failover proxy for the active provider plugin")
    proxy_parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local proxy to")
    proxy_parser.add_argument("--port", type=int, default=4141, help="Port to bind the local proxy to")

    status_parser = subparsers.add_parser("status", help="Show overall account and proxy runtime status")
    status_parser.add_argument("--json", action="store_true", dest="json_output")

    list_parser = subparsers.add_parser("list", help="List saved accounts")
    list_parser.add_argument("--json", action="store_true", dest="json_output")

    login_parser = subparsers.add_parser("login", help="Login with the selected provider plugin in the browser by default")
    login_parser.add_argument("--provider", default=None, help="Provider plugin id to use for login")
    login_parser.add_argument("--label", default=None, help="Optional account label")
    login_parser.add_argument("--headless", action="store_true", help="Use manual device-code login instead of browser callback login")
    switch_parser = subparsers.add_parser("switch", help="Switch the active local account")
    switch_parser.add_argument("account", help="Account number, label, account_id, or internal id")

    return parser


def print_help_command() -> int:
    print("PunkRecords")
    print("Primary CLI: punkrecords")
    print("What this tool does:")
    print("- stores accounts, settings, and proxy stats in a project-local runtime by default")
    print("- runs a local failover proxy backed by saved provider plugin logins")
    print("- lets you inspect and manage the active account from the CLI")
    print()
    print("Default runtime paths:")
    print(f"- runtime root: {app_home()}")
    print(f"- accounts:     {app_home() / 'accounts.json'}")
    print("- env overrides: PUNKRECORDS_HOME")
    print()
    print("Recommended flow:")
    print("1. Start the local proxy runtime when you need it:")
    print("   uv run punkrecords proxy --host 127.0.0.1 --port 4141")
    print()
    print("2. Login in the browser:")
    print("   uv run punkrecords login --label work")
    print("   uv run punkrecords login --provider openai-codex --label work")
    print()
    print("3. If browser login is not possible, use headless fallback:")
    print("   uv run punkrecords login --headless --label backup")
    print("   uv run punkrecords login --provider openai-codex --headless --label backup")
    print()
    print("4. Check current status:")
    print("   uv run punkrecords status")
    print()
    print("5. List saved accounts:")
    print("   uv run punkrecords list")
    print()
    print("6. Open the arrow-key TUI:")
    print("   uv run punkrecords tui")
    print()
    print("7. Switch the active account:")
    print("   uv run punkrecords switch 2")
    print()
    print("Useful commands:")
    print("- help")
    print("- login [--provider ID] [--label NAME] [--headless]")
    print("- list [--json]")
    print("- status [--json]")
    print("- tui")
    print("- proxy [--host HOST] [--port PORT]")
    print("- switch <account>")
    return 0


def account_summary(account: AccountRecord, *, active_id: str | None) -> dict[str, object]:
    return {
        "id": account.id,
        "account_id": account.external_id,
        "email": account.contact,
        "label": account.display_name,
        "provider": account.provider,
        "active": account.id == active_id,
        "last_refresh": account.last_refresh,
        "last_used": account.last_used,
        "enabled": account.enabled,
    }


def get_fetch_account_usage() -> Callable[[AccountRecord], tuple[AccountRecord, Any]]:
    def _fetch(account: AccountRecord) -> tuple[AccountRecord, Any]:
        provider = get_account_provider(account)
        if provider.usage is None:
            return account, AccountUsage(account_id=account.account_id, label=account.label, provider=account.provider, error="usage capability unavailable")
        return require_usage_provider(provider).fetch_account_usage(account)

    return _fetch


def build_status_payload(repo: AccountRepository) -> dict[str, Any]:
    fetch_account_usage = get_fetch_account_usage()
    state = repo.load()
    active = repo.get_active()
    refreshed_accounts: list[AccountRecord] = []
    usages_by_provider: dict[str, list[Any]] = {}

    for account in state.accounts:
        refreshed_account, usage = fetch_account_usage(account)
        refreshed_accounts.append(refreshed_account)
        usages_by_provider.setdefault(refreshed_account.provider, []).append(usage)

    for account in refreshed_accounts:
        repo.upsert_account(account, make_active=(account.id == state.active_account_id))

    usage_reports = []
    for provider_id, usages in usages_by_provider.items():
        provider_info = get_provider(provider_id)
        if provider_info.usage is None:
            usage_reports.append(
                {
                    "provider_id": provider_info.provider_id,
                    "title": provider_info.label,
                    "subtitle": "Usage reporting is not available for this provider.",
                    "cards": [],
                    "columns": ["Provider", "Status"],
                    "rows": [[provider_info.provider_id, "usage capability unavailable"]],
                    "table_lines": ["usage capability unavailable"],
                    "summary_lines": [],
                    "summary": {},
                }
            )
            continue
        provider = require_usage_provider(provider_info)
        usage_reports.append(provider.build_usage_report(usages))

    return {
        "accounts_path": str(accounts_path()),
        "account_count": len(state.accounts),
        "active_account": account_summary(active, active_id=state.active_account_id) if active else None,
        "usage_reports": usage_reports,
    }


def print_status(repo: AccountRepository, *, json_output: bool = False) -> int:
    payload = build_status_payload(repo)

    if json_output:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Accounts store: {payload['accounts_path']}")
    print(f"Accounts:      {payload['account_count']}")
    active_account = payload["active_account"]
    if active_account:
        print(f"Active:        {active_account['label']} ({active_account['email'] or active_account['account_id']})")
    else:
        print("Active:        none")
    print()
    for report in payload["usage_reports"]:
        print()
        print(report["title"])
        for line in report["table_lines"]:
            print(line)
        if report.get("summary_lines"):
            print()
            print("Summary:")
            for line in report["summary_lines"]:
                print(line)
    return 0


def print_list(repo: AccountRepository, *, json_output: bool = False) -> int:
    state = repo.load()
    accounts = [account_summary(account, active_id=state.active_account_id) for account in state.accounts]
    if json_output:
        print(json.dumps(accounts, indent=2))
        return 0

    if not accounts:
        print("No accounts saved yet.")
        return 0

    for index, account in enumerate(accounts, start=1):
        marker = "*" if account["active"] else " "
        identity = account["email"] or account["account_id"]
        print(f"{marker} {index}. {account['label']} — {identity}")
    return 0


def handle_login(args: argparse.Namespace, repo: AccountRepository) -> int:
    provider_info = get_provider(args.provider)
    if provider_info.auth is None:
        print(f"Provider {provider_info.provider_id} does not support login.", file=sys.stderr)
        return 1
    provider = require_auth_provider(provider_info)
    try:
        if args.headless:
            result = provider.login_via_device_flow(label=args.label, headless=True)
        else:
            result = provider.login_via_browser_flow(label=args.label)
    except OAuthError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    account = repo.upsert_account(result.account, make_active=True)
    print()
    print(f"Saved account: {account.display_name}")
    print(f"Account id:    {account.external_id}")
    if account.contact:
        print(f"Email:         {account.contact}")

    return 0


def handle_switch(args: argparse.Namespace, repo: AccountRepository) -> int:
    try:
        account = repo.set_active(args.account)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Active account: {account.display_name} ({account.contact or account.external_id})")
    return 0


def _capture_output(callback: Callable[[], int]) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = callback()
    output = buffer.getvalue().strip()
    if exit_code not in (0, None):
        return output or f"Command failed with exit code {exit_code}"
    return output or "Done."


def handle_tui(repo: AccountRepository) -> int:
    def action_runner(action: str) -> str:
        if action == "status":
            return _capture_output(lambda: print_status(repo, json_output=False))
        if action == "list":
            return _capture_output(lambda: print_list(repo, json_output=False))
        if action == "login_browser":
            provider_id = select_provider_id()
            if provider_id is None:
                return "No provider selected."
            return _capture_output(lambda: handle_login(argparse.Namespace(provider=provider_id, label=None, headless=False), repo))
        if action == "login_headless":
            provider_id = select_provider_id()
            if provider_id is None:
                return "No provider selected."
            return _capture_output(lambda: handle_login(argparse.Namespace(provider=provider_id, label=None, headless=True), repo))
        if action == "switch":
            selected_index = select_account_index(repo)
            if selected_index is None:
                return "No account selected."
            return _capture_output(lambda: handle_switch(argparse.Namespace(account=str(selected_index + 1)), repo))
        if action == "help":
            return _capture_output(print_help_command)
        return "Unknown action."

    return run_tui(repo, action_runner)


def handle_proxy(args: argparse.Namespace, repo: AccountRepository) -> int:
    return run_proxy_server(repo, host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = AccountRepository()

    if args.command == "help":
        return print_help_command()
    if args.command == "tui":
        return handle_tui(repo)
    if args.command == "proxy":
        return handle_proxy(args, repo)
    if args.command == "status":
        return print_status(repo, json_output=args.json_output)
    if args.command == "list":
        return print_list(repo, json_output=args.json_output)
    if args.command == "login":
        return handle_login(args, repo)
    if args.command == "switch":
        return handle_switch(args, repo)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
