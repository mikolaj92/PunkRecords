from __future__ import annotations

import argparse
import curses
from dataclasses import dataclass
from typing import Callable

from .providers import supported_provider_metadata
from .store import AccountRepository


@dataclass(frozen=True)
class MenuAction:
    label: str
    handler: Callable[[], str]


def move_selection(index: int, key: int, item_count: int) -> int:
    if item_count <= 0:
        return 0
    if key == curses.KEY_UP:
        return (index - 1) % item_count
    if key == curses.KEY_DOWN:
        return (index + 1) % item_count
    return index


def build_account_lines(repo: AccountRepository) -> list[str]:
    state = repo.load()
    if not state.accounts:
        return ["No accounts saved yet."]

    lines: list[str] = []
    for index, account in enumerate(state.accounts, start=1):
        marker = "*" if account.id == state.active_account_id else " "
        identity = account.contact or account.external_id
        lines.append(f"{marker} {index}. {account.display_name} — {identity}")
    return lines


def _run_selector(stdscr: curses.window, title: str, items: list[str]) -> int | None:
    curses.curs_set(0)
    selected = 0
    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, title)
        stdscr.addstr(1, 0, "Use ↑/↓, Enter to confirm, q to cancel")
        for row, item in enumerate(items, start=3):
            prefix = "> " if row - 3 == selected else "  "
            stdscr.addstr(row, 0, f"{prefix}{item}")
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (10, 13, curses.KEY_ENTER):
            return selected
        selected = move_selection(selected, key, len(items))


def select_account_index(repo: AccountRepository) -> int | None:
    items = build_account_lines(repo)
    if items == ["No accounts saved yet."]:
        return None
    return curses.wrapper(lambda stdscr: _run_selector(stdscr, "Select active account", items))


def select_provider_id() -> str | None:
    providers = supported_provider_metadata()
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]["id"]
    index = curses.wrapper(lambda stdscr: _run_selector(stdscr, "Select provider", [f"{item['label']} ({item['id']})" for item in providers]))
    if index is None:
        return None
    return providers[index]["id"]


def run_tui(repo: AccountRepository, action_runner: Callable[[str], str]) -> int:
    actions = [
        MenuAction("Status", lambda: action_runner("status")),
        MenuAction("List accounts", lambda: action_runner("list")),
        MenuAction("Login in browser", lambda: action_runner("login_browser")),
        MenuAction("Login headless", lambda: action_runner("login_headless")),
        MenuAction("Switch active account", lambda: action_runner("switch")),
        MenuAction("Help", lambda: action_runner("help")),
        MenuAction("Quit", lambda: "Bye."),
    ]
    labels = [action.label for action in actions]
    selected = 0
    result_lines = ["Ready."]

    def _render(stdscr: curses.window, loading: bool = False) -> None:
        nonlocal selected, result_lines
        curses.curs_set(0)
        stdscr.clear()
        stdscr.addstr(0, 0, "PunkRecords TUI")
        stdscr.addstr(1, 0, "Use ↑/↓, Enter to run, q to quit")
        for row, item in enumerate(labels, start=3):
            prefix = "> " if row - 3 == selected else "  "
            stdscr.addstr(row, 0, f"{prefix}{item}")

        output_start = len(labels) + 5
        stdscr.addstr(output_start, 0, "Output:")
        if loading:
            stdscr.addstr(output_start + 1, 0, "Loading...")
        else:
            for index, line in enumerate(result_lines[: max(1, curses.LINES - output_start - 2)], start=1):
                stdscr.addstr(output_start + index, 0, line[: max(1, curses.COLS - 1)])
        stdscr.refresh()

    while True:
        def _loop(stdscr: curses.window) -> int:
            nonlocal selected, result_lines
            while True:
                _render(stdscr, loading=False)
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    return -1
                if key in (10, 13, curses.KEY_ENTER):
                    return selected
                selected = move_selection(selected, key, len(labels))

        selection = curses.wrapper(_loop)
        if selection < 0:
            return 0

        action = actions[selection]
        if action.label == "Quit":
            return 0

        def _run_action(stdscr: curses.window) -> None:
            _render(stdscr, loading=True)

        curses.wrapper(_run_action)
        output = action.handler().strip() or "Done."
        result_lines = output.splitlines() or ["Done."]
