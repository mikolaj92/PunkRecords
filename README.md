# PunkRecords

PunkRecords is a self-contained local proxy runtime with plugin-based provider support and built-in multi-account OAuth management.

This project keeps **many provider logins** in its own local store and exposes a CLI for:

- logging in with a browser-backed device flow,
- logging in with a browser-open OAuth callback flow,
- listing saved accounts,
- checking status,
- showing combined 5h and weekly usage totals across accounts,
- switching the active account,
- opening a simple arrow-key TUI,
- running a local OpenAI-compatible failover proxy for supported routes,

## Why this exists

PunkRecords keeps provider logins, failover state, and proxy telemetry together under one local runtime root while exposing a single proxy surface for traffic.

## Current scope

Version `0.1.0` ships with one built-in provider plugin:

- built-in provider: `openai-codex`
- auth type: OAuth logins, not API keys
- CLI-first workflow
- staged OpenAI-compatible proxy support for selected routes

The provider system itself is plugin-based. Built-in plugins live in the repository under the `providers/` package, and external plugins can be loaded by setting:

- `PUNKRECORDS_PROVIDER_MODULES=my_provider_module,another_provider_module`

## Commands

```bash
punkrecords status
punkrecords list
punkrecords login --label work
punkrecords login --headless --label backup
punkrecords switch 2
punkrecords tui
punkrecords proxy --host 127.0.0.1 --port 4141
```

## Development

```bash
uv venv
uv pip install -e . pytest
uv run pytest
```

You can also run without installing:

```bash
uv run punkrecords status
```

## Storage

By default the project stores its state in:

- `./.punkrecords/accounts.json`
- `./.punkrecords/settings.json`
- `./.punkrecords/stats/proxy-rollups.json`
- `./.punkrecords/stats/proxy-requests.jsonl`

You can override that root directory with the new primary environment variable:

- `PUNKRECORDS_HOME=/path/to/home`

## Login flow

The built-in `openai-codex` provider uses the OpenAI Codex browser-based OAuth flow with a local loopback callback.

- default mode: opens the browser to the authorize URL and waits for the local callback
- `--headless`: uses the manual device-code flow as a fallback

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

FastAPI exposes built-in API docs and schema when the proxy is running:

- `GET /openapi.json`
- `GET /docs`
- `GET /redoc`

The proxy selects a healthy saved account, forwards the request upstream, and fails over to the next eligible account on qualifying transient/account-scoped failures such as `deactivated_workspace`.

Current compatibility notes:

- non-streaming requests are supported for both proxied routes
- live streaming passthrough is supported for `stream=true` requests on both supported routes
- non-streaming embeddings requests are supported through `/v1/embeddings`
- local proxy stats are stored on disk and exposed through `/_proxy/stats/summary`
- `/v1/models` is available as a minimal compatibility discovery route
- this is not yet a universal drop-in replacement for every OpenAI API endpoint
