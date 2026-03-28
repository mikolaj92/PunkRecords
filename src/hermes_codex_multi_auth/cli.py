from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from typing import Any, Callable

from hermes_codex_multi_auth.hermes_bridge import hermes_synced_account_id, sync_account_to_hermes
from hermes_codex_multi_auth.models import AccountRecord
from hermes_codex_multi_auth.oauth import OAuthError, login_via_browser_flow, login_via_device_flow, maybe_refresh_account
from hermes_codex_multi_auth.paths import accounts_path, app_home, hermes_auth_path
from hermes_codex_multi_auth.proxy import run_proxy_server
from hermes_codex_multi_auth.store import AccountRepository
from hermes_codex_multi_auth.tui import select_account_index, run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="punkrecords", description="Run a self-contained local openai-codex proxy runtime with multi-account OAuth state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="Show practical usage instructions")
    subparsers.add_parser("tui", help="Open arrow-key TUI menu")
    proxy_parser = subparsers.add_parser("proxy", help="Run local openai-codex failover proxy")
    proxy_parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local proxy to")
    proxy_parser.add_argument("--port", type=int, default=4141, help="Port to bind the local proxy to")

    status_parser = subparsers.add_parser("status", help="Show overall account and Hermes sync status")
    status_parser.add_argument("--json", action="store_true", dest="json_output")

    list_parser = subparsers.add_parser("list", help="List saved accounts")
    list_parser.add_argument("--json", action="store_true", dest="json_output")

    login_parser = subparsers.add_parser("login", help="Login with OpenAI Codex OAuth in the browser by default")
    login_parser.add_argument("--label", default=None, help="Optional account label")
    login_parser.add_argument("--headless", action="store_true", help="Use manual device-code login instead of browser callback login")
    login_parser.add_argument("--sync", action="store_true", help="Sync the newly active account to Hermes auth.json")

    switch_parser = subparsers.add_parser("switch", help="Switch the active local account")
    switch_parser.add_argument("account", help="Account number, label, account_id, or internal id")
    switch_parser.add_argument("--sync", action="store_true", help="Sync the newly active account to Hermes auth.json")

    sync_parser = subparsers.add_parser("sync", help="Sync an account into Hermes auth.json")
    sync_parser.add_argument("account", nargs="?", help="Optional account number, label, account_id, or internal id")

    return parser


def print_help_command() -> int:
    print("PunkRecords")
    print("Primary CLI: punkrecords")
    print("Compatibility CLI: hermes-codex-auth")
    print()
    print("What this tool does:")
    print("- stores accounts, settings, stats, and Hermes auth payloads in a project-local runtime by default")
    print("- runs a local failover proxy backed by those saved openai-codex OAuth logins")
    print("- lets you inspect and sync the active account from the CLI")
    print()
    print("Default runtime paths:")
    print(f"- runtime root: {app_home()}")
    print(f"- accounts:     {app_home() / 'accounts.json'}")
    print(f"- Hermes auth:  {hermes_auth_path()}")
    print("- env overrides: PUNKRECORDS_HOME, HERMES_CODEX_MULTI_AUTH_HOME, HERMES_HOME")
    print()
    print("Recommended flow:")
    print("1. Start the local proxy runtime when you need it:")
    print("   uv run punkrecords proxy --host 127.0.0.1 --port 4141")
    print()
    print("2. Login in the browser:")
    print("   uv run punkrecords login --label work")
    print()
    print("3. If browser login is not possible, use headless fallback:")
    print("   uv run punkrecords login --headless --label backup")
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
    print("8. Sync the active account into the default Hermes payload if needed:")
    print("   uv run punkrecords sync")
    print()
    print("Legacy compatibility:")
    print("- hermes-codex-auth remains available as an alias")
    print()
    print("Useful commands:")
    print("- help")
    print("- login [--label NAME] [--headless] [--sync]")
    print("- list [--json]")
    print("- status [--json]")
    print("- tui")
    print("- proxy [--host HOST] [--port PORT]")
    print("- switch <account> [--sync]")
    print("- sync [account]")
    return 0


def account_summary(account: AccountRecord, *, active_id: str | None) -> dict[str, object]:
    return {
        "id": account.id,
        "account_id": account.account_id,
        "email": account.email,
        "label": account.label,
        "active": account.id == active_id,
        "last_refresh": account.last_refresh,
        "last_used": account.last_used,
        "enabled": account.enabled,
    }


def aggregate_usage_totals(usages: list[dict[str, Any]]) -> dict[str, Any]:
    def _window_summary(key: str) -> dict[str, Any]:
        reported = 0
        used_percent_total = 0.0
        reset_after_seconds_min: int | None = None
        reset_at_min: int | None = None
        for usage in usages:
            if usage.get("error"):
                continue
            window = usage.get(key) or {}
            used_percent = window.get("used_percent")
            if not isinstance(used_percent, (int, float)):
                continue
            reported += 1
            used_percent_total += float(used_percent)
            reset_after_seconds = window.get("reset_after_seconds")
            if isinstance(reset_after_seconds, (int, float)):
                value = int(reset_after_seconds)
                reset_after_seconds_min = value if reset_after_seconds_min is None else min(reset_after_seconds_min, value)
            reset_at = window.get("reset_at")
            if isinstance(reset_at, (int, float)):
                value = int(reset_at)
                reset_at_min = value if reset_at_min is None else min(reset_at_min, value)
        return {
            "used_percent_total": round(used_percent_total, 2),
            "capacity_percent_total": reported * 100.0,
            "reported_accounts": reported,
            "reset_after_seconds_min": reset_after_seconds_min,
            "reset_after_seconds": reset_after_seconds_min,
            "reset_at": reset_at_min,
        }

    failed_accounts = [usage["label"] for usage in usages if usage.get("error")]
    return {
        "5h": _window_summary("primary_window"),
        "weekly": _window_summary("secondary_window"),
        "failed_accounts": failed_accounts,
    }


def format_reset(window: dict[str, Any]) -> str:
    reset_after_seconds = window.get("reset_after_seconds")
    reset_at = window.get("reset_at")

    parts: list[str] = []
    if isinstance(reset_after_seconds, (int, float)):
        seconds = int(reset_after_seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")

    reset_at_text = None
    if isinstance(reset_at, (int, float)):
        reset_at_text = datetime.fromtimestamp(int(reset_at), tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")

    if parts and reset_at_text:
        return f"reset in {' '.join(parts)} at {reset_at_text}"
    if parts:
        return f"reset in {' '.join(parts)}"
    if reset_at_text:
        return f"reset at {reset_at_text}"
    return "reset unknown"


def _table_separator(widths: list[int]) -> str:
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def _table_row(values: list[str], widths: list[int]) -> str:
    padded = [f" {value.ljust(width)} " for value, width in zip(values, widths, strict=True)]
    return "|" + "|".join(padded) + "|"


def build_usage_table(usages: list[dict[str, Any]]) -> list[str]:
    headers = ["Account", "5h", "5h reset", "Week", "Week reset"]
    rows: list[list[str]] = []
    for usage in usages:
        label = str(usage["label"])
        if usage.get("error"):
            rows.append([label, "error", str(usage["error"]), "error", str(usage["error"])])
            continue

        primary = usage.get("primary_window") or {}
        secondary = usage.get("secondary_window") or {}
        primary_used = primary.get("used_percent")
        secondary_used = secondary.get("used_percent")
        rows.append(
            [
                label,
                f"{primary_used}%" if isinstance(primary_used, (int, float)) else "unknown",
                format_reset(primary),
                f"{secondary_used}%" if isinstance(secondary_used, (int, float)) else "unknown",
                format_reset(secondary),
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    lines = [_table_separator(widths), _table_row(headers, widths), _table_separator(widths)]
    lines.extend(_table_row(row, widths) for row in rows)
    lines.append(_table_separator(widths))
    return lines


def get_fetch_account_usage() -> Callable[[AccountRecord], tuple[AccountRecord, Any]]:
    usage_module = importlib.import_module("hermes_codex_multi_auth.usage")
    return usage_module.fetch_account_usage


def build_status_payload(repo: AccountRepository) -> dict[str, Any]:
    fetch_account_usage = get_fetch_account_usage()
    state = repo.load()
    active = repo.get_active()
    synced_account_id = hermes_synced_account_id()
    refreshed_accounts: list[AccountRecord] = []
    usage_snapshots: list[dict[str, Any]] = []

    for account in state.accounts:
        refreshed_account, usage = fetch_account_usage(account)
        refreshed_accounts.append(refreshed_account)
        usage_snapshots.append(usage.to_dict())

    for account in refreshed_accounts:
        repo.upsert_account(account, make_active=(account.id == state.active_account_id))

    return {
        "accounts_path": str(accounts_path()),
        "hermes_auth_path": str(hermes_auth_path()),
        "account_count": len(state.accounts),
        "active_account": account_summary(active, active_id=state.active_account_id) if active else None,
        "hermes_synced_account_id": synced_account_id,
        "in_sync": bool(active and synced_account_id == active.account_id),
        "usage_totals": aggregate_usage_totals(usage_snapshots),
        "usage_by_account": usage_snapshots,
    }


def print_status(repo: AccountRepository, *, json_output: bool = False) -> int:
    payload = build_status_payload(repo)

    if json_output:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Accounts store: {payload['accounts_path']}")
    print(f"Hermes auth:   {payload['hermes_auth_path']}")
    print(f"Accounts:      {payload['account_count']}")
    usage_totals = payload["usage_totals"]
    five_hour = usage_totals["5h"]
    weekly = usage_totals["weekly"]
    active_account = payload["active_account"]
    if active_account:
        print(f"Active:        {active_account['label']} ({active_account['email'] or active_account['account_id']})")
    else:
        print("Active:        none")
    print(f"Hermes sync:   {'yes' if payload['in_sync'] else 'no'}")
    synced_account_id = payload["hermes_synced_account_id"]
    if synced_account_id:
        print(f"Synced id:     {synced_account_id}")

    print()
    print("Per-account usage:")
    for line in build_usage_table(payload["usage_by_account"]):
        print(line)

    print()
    print("Summary:")
    print(f"Reported 5h:   {five_hour['used_percent_total']}% / {five_hour['capacity_percent_total']}% across {five_hour['reported_accounts']} account(s), {format_reset(five_hour)}")
    print(f"Reported week: {weekly['used_percent_total']}% / {weekly['capacity_percent_total']}% across {weekly['reported_accounts']} account(s), {format_reset(weekly)}")
    if usage_totals["failed_accounts"]:
        print(f"Usage errors:  {', '.join(usage_totals['failed_accounts'])}")
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
    try:
        if args.headless:
            result = login_via_device_flow(label=args.label, headless=True)
        else:
            result = login_via_browser_flow(label=args.label)
    except OAuthError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    account = repo.upsert_account(result.account, make_active=True)
    print()
    print(f"Saved account: {account.label}")
    print(f"Account id:    {account.account_id}")
    if account.email:
        print(f"Email:         {account.email}")

    if args.sync:
        path = sync_account_to_hermes(account)
        print(f"Synced to:     {path}")
    return 0


def handle_switch(args: argparse.Namespace, repo: AccountRepository) -> int:
    try:
        account = repo.set_active(args.account)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Active account: {account.label} ({account.email or account.account_id})")
    if args.sync:
        path = sync_account_to_hermes(account)
        print(f"Synced to:      {path}")
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
            return _capture_output(lambda: handle_login(argparse.Namespace(label=None, headless=False, sync=False), repo))
        if action == "login_headless":
            return _capture_output(lambda: handle_login(argparse.Namespace(label=None, headless=True, sync=False), repo))
        if action == "switch":
            selected_index = select_account_index(repo)
            if selected_index is None:
                return "No account selected."
            return _capture_output(lambda: handle_switch(argparse.Namespace(account=str(selected_index + 1), sync=False), repo))
        if action == "sync":
            return _capture_output(lambda: handle_sync(argparse.Namespace(account=None), repo))
        if action == "help":
            return _capture_output(print_help_command)
        return "Unknown action."

    return run_tui(repo, action_runner)


def handle_proxy(args: argparse.Namespace, repo: AccountRepository) -> int:
    return run_proxy_server(repo, host=args.host, port=args.port)


def handle_sync(args: argparse.Namespace, repo: AccountRepository) -> int:
    try:
        account = repo.resolve_account(args.account) if args.account else repo.get_active()
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if account is None:
        print("No active account to sync.", file=sys.stderr)
        return 1

    try:
        account = maybe_refresh_account(account)
    except OAuthError as exc:
        print(f"Refresh failed before sync: {exc}", file=sys.stderr)
        return 1

    repo.upsert_account(account, make_active=(args.account is None or account.id == repo.load().active_account_id))
    path = sync_account_to_hermes(account)
    print(f"Synced account {account.label} to {path}")
    return 0


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
    if args.command == "sync":
        return handle_sync(args, repo)

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
