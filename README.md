# PunkRecords

PunkRecords is a self-contained local proxy runtime for `openai-codex`, with built-in multi-account OAuth management designed to work with Hermes.

This project keeps **many ChatGPT/Codex OAuth logins** in its own local store and exposes a CLI for:

- logging in with a browser-backed device flow,
- logging in with a browser-open OAuth callback flow,
- listing saved accounts,
- checking status,
- showing combined 5h and weekly usage totals across accounts,
- switching the active account,
- opening a simple arrow-key TUI,
- running a local OpenAI-compatible failover proxy for supported routes,
- syncing the active account into the runtime-local Hermes auth payload by default.

Hermes itself still consumes **one active Codex auth payload**. PunkRecords keeps that payload, the account store, settings, and proxy stats together under one project-local runtime root by default.

## Why this exists

Hermes currently consumes a single `openai-codex` auth payload. That works for one login, but not for a proxy-first workflow where you want multiple OAuth logins, local failover, and a runtime you can keep inside the repo. This project fills that gap without rewriting Hermes core first.

## Current scope

Version `0.1.0` intentionally targets only:

- provider: `openai-codex`
- auth type: OAuth logins, not API keys
- CLI-first workflow
- staged OpenAI-compatible proxy support for selected routes

## Commands

```bash
punkrecords status
punkrecords list
punkrecords login --label work
punkrecords login --headless --label backup
punkrecords switch 2
punkrecords tui
punkrecords proxy --host 127.0.0.1 --port 4141
punkrecords sync
```

The legacy CLI name still works for compatibility:

```bash
hermes-codex-auth status
```

## Development

```bash
uv venv
uv pip install -e . pytest
uv run pytest
```

You can also run without installing:

```bash
uv run python -m hermes_codex_multi_auth.cli status
```

## Storage

By default the project stores its state in:

- `./.punkrecords/accounts.json`
- `./.punkrecords/settings.json`
- `./.punkrecords/stats/proxy-rollups.json`
- `./.punkrecords/stats/proxy-requests.jsonl`
- `./.punkrecords/hermes/auth.json`

You can override that root directory with the new primary environment variable:

- `PUNKRECORDS_HOME=/path/to/home`

The legacy env var still works for compatibility:

- `HERMES_CODEX_MULTI_AUTH_HOME=/path/to/home`

If you want Hermes auth somewhere else, `HERMES_HOME=/path/to/hermes-home` still overrides the default and writes to `/path/to/hermes-home/auth.json`.

## Login flow

The default login command uses the OpenAI Codex browser-based OAuth flow with a local loopback callback.

- default mode: opens the browser to the authorize URL and waits for the local callback
- `--headless`: uses the manual device-code flow as a fallback

## Hermes sync

`sync` writes the currently active account into:

- `./.punkrecords/hermes/auth.json`

`HERMES_HOME` still overrides that location when needed. The sync logic preserves unrelated providers and updates only the `openai-codex` provider state.

## TUI

Run:

```bash
uv run punkrecords tui
```

Use the arrow keys and Enter to navigate the menu.

## Local proxy

The proxy server is implemented with FastAPI and served through Uvicorn.

PunkRecords is designed to behave primarily as a self-contained proxy server, so the default runtime state stays inside the repo under `./.punkrecords/`.

Run:

```bash
uv run punkrecords proxy --host 127.0.0.1 --port 4141
```

Available routes in v1:

- `GET /healthz`
- `GET /_proxy/stats/summary`
- `GET /_proxy/admin/state`
- `GET /_proxy/admin/accounts`
- `GET /_proxy/admin/requests`
- `GET /_proxy/admin/settings`
- `PUT /_proxy/admin/settings`
- `PATCH /_proxy/admin/settings`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/embeddings`

The proxy selects a healthy saved account, forwards the request upstream, and fails over to the next eligible account on qualifying transient/account-scoped failures such as `deactivated_workspace`.

Current compatibility notes:

- non-streaming requests are supported for both proxied routes
- live streaming passthrough is supported for `stream=true` requests on both supported routes
- non-streaming embeddings requests are supported through `/v1/embeddings`
- local proxy stats are stored on disk and exposed through `/_proxy/stats/summary`
- read-only/read-mostly admin endpoints are available as groundwork for a future dashboard
- `/v1/models` is available as a minimal compatibility discovery route
- this is not yet a universal drop-in replacement for every OpenAI API endpoint
