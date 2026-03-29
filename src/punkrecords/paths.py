from __future__ import annotations

import os
from pathlib import Path


APP_HOME_ENV_VAR = "PUNKRECORDS_HOME"
DEFAULT_APP_HOME_DIRNAME = ".punkrecords"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def app_home() -> Path:
    override = os.getenv(APP_HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()

    return project_root() / DEFAULT_APP_HOME_DIRNAME


def ensure_app_home() -> Path:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def accounts_path() -> Path:
    return ensure_app_home() / "accounts.json"


def stats_dir() -> Path:
    path = ensure_app_home() / "stats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def proxy_rollups_path() -> Path:
    return stats_dir() / "proxy-rollups.json"


def proxy_requests_path() -> Path:
    return stats_dir() / "proxy-requests.jsonl"


def settings_path() -> Path:
    return ensure_app_home() / "settings.json"
