from __future__ import annotations

import curses
import importlib

models_module = importlib.import_module("hermes_codex_multi_auth.models")
store_module = importlib.import_module("hermes_codex_multi_auth.store")
tui_module = importlib.import_module("hermes_codex_multi_auth.tui")

AccountRecord = models_module.AccountRecord
AccountTokens = models_module.AccountTokens
AccountRepository = store_module.AccountRepository
build_account_lines = tui_module.build_account_lines
move_selection = tui_module.move_selection
MenuAction = tui_module.MenuAction


def _account(index: int, label: str) -> AccountRecord:
    return AccountRecord(
        id=f"local-{index}",
        account_id=f"acct-{index}",
        email=f"user{index}@example.com",
        label=label,
        created_at="2026-03-27T00:00:00Z",
        last_refresh="2026-03-27T00:00:00Z",
        last_used="2026-03-27T00:00:00Z",
        tokens=AccountTokens(
            access_token="token",
            refresh_token=f"refresh-{index}",
            account_id=f"acct-{index}",
        ),
    )


def test_move_selection_wraps() -> None:
    assert move_selection(0, curses.KEY_UP, 3) == 2
    assert move_selection(2, curses.KEY_DOWN, 3) == 0
    assert move_selection(1, ord("x"), 3) == 1


def test_build_account_lines_marks_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUNKRECORDS_HOME", str(tmp_path / "manager"))
    repo = AccountRepository()
    repo.upsert_account(_account(1, "work"), make_active=True)
    repo.upsert_account(_account(2, "backup"), make_active=False)

    lines = build_account_lines(repo)

    assert lines == [
        "* 1. work — user1@example.com",
        "  2. backup — user2@example.com",
    ]


def test_menu_action_dataclass() -> None:
    action = MenuAction("Status", lambda: "ok")
    assert action.label == "Status"
    assert action.handler() == "ok"
