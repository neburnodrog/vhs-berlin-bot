# vhs-berlin-bot

A single-user Telegram bot that scrapes Berlin VHS (Volkshochschulen) for newly-bookable courses
matching whitelisted users' keywords. Runs one daily scan at 08:00 Europe/Berlin.

## Build & Development

- Install deps: `uv sync`
- Run bot locally: `uv run vhsbot` (requires `.env` with `TELEGRAM_BOT_TOKEN` + `ALLOWED_USER_IDS`)
- Run all tests: `uv run pytest`
- Single test file: `uv run pytest tests/test_handlers.py`
- Single test pattern: `uv run pytest -k "test_watch_backfill"`
- Lint: `uv run ruff check`
- Lint + autofix: `uv run ruff check --fix`
- Format: `uv run ruff format`
- Docker build: `docker build -t vhs-berlin-bot .`

Always use `uv` — never bare `pip` or `python` directly. `uv run <cmd>` executes inside the project's venv.

## Code Style

Ruff handles formatting/imports/most lint (line-length 100, double quotes, py313 target, selects `E/F/I/UP/B/SIM/RUF`).
Beyond ruff:

- New `.py` files MUST start with `from __future__ import annotations`. The whole codebase uses this.
- Module-level docstrings document the **WHY**, including Phase-N decisions and rationale (see `src/vhsbot/main.py` for an example). When adding a module, explain the design choice, not just what the code does.
- Shared resources (httpx client, SQLite conn, settings, async lock) live on `application.bot_data` keyed by constants defined in `src/vhsbot/_app_state.py`. **Never** introduce globals or scatter raw string keys. Add new keys to `_app_state.py` first.

## Project Structure

- `src/vhsbot/` — bot source (12 modules, layered: config → db / scraper / parser / matching / diff / formatting → handlers / jobs → main)
- `tests/` — pytest suite, one file per source module + `test_e2e.py` for cross-module flows
- `tests/fixtures/` — real captured HTML from vhsit.berlin.de (see `tests/fixtures/README.md` for capture report)
- `tasks/todo .md` — locked design decisions + recon facts; the project's spec of record
- `data/` — runtime state: `vhsbot.db` (SQLite) + `snapshots/YYYY-MM-DD/*.html` (gitignored, prunes after 7 days)
- `Dockerfile` — uv builder → `python:3.13-slim` runtime, runs as `vhsbot` uid 1000

See ARCHITECTURE.md for the module map, entry points, and invariants.

## Workflow

This is a single-user personal project. Direct-to-main flow — no PRs, no CI.

- **Commit format:** `Phase N <area>: <description>`
  - Examples: `Phase 7 deploy: pre-create /data with vhsbot ownership`, `Phase 5: daily-scan job + cross-day cap + snapshot persistence`
- **Review-fix commits:** `Phase N review fixes [round M]: <items>` — for follow-up fixes to a Phase commit, e.g. `Phase 7 scraper review-fix round 2: make MAX_PAGES_GUARD public, immutable snapshots, boundary tests`
- Before committing: run `uv run ruff check --fix && uv run ruff format && uv run pytest` and ensure all pass.

## Gotchas

- **VHS encoding lies.** The HTTP `Content-Type` header claims `iso-8859-15` but the page is actually `windows-1252`. Save `response.content` raw and decode as `windows-1252` explicitly. Anything else mangles umlauts.
- **VHS session timeout: 29.5 min.** Each scrape session must complete inside this window. The scraper uses one shared `httpx.AsyncClient` per scan to preserve cookies; don't shortcut by creating per-request clients.
- **`/watch` blocks synchronously by design.** The handler awaits `scraper.crawl` and only then replies — user sees a typing indicator for up to ~60s. We chose this over fire-and-forget because the bot is single-user (no handler contention) and the blocking flow avoids orphan-task lifetime management. Do not convert this to a background task without a strong reason.
- **AIORateLimiter throttles ALL outbound sends** — including the 15-msg/day backfill burst. PTB defaults (~30 req/s) sit inside Telegram's limits; do NOT pass custom `rate_limit_args` per call.
- **ConversationHandler PTBUserWarning is intentionally suppressed** (see `pyproject.toml` `filterwarnings`). The onboarding flow deliberately mixes `CallbackQueryHandler` with `per_message=False`. Don't "fix" the warning by changing this — the configuration is correct for our UX.
- **Manual `/scan` defers to daily scan.** Both handlers share the `daily_scan` callback and check the `scan_running` bot_data flag to avoid concurrent runs.
- **`/data` ownership in container.** The Dockerfile pre-creates `/data` and chowns it to uid 1000 (`vhsbot` user). If you change the runtime user or mount paths, the bot will fail to write its SQLite DB at boot.
- **`tests/fixtures/` is ground truth.** These are real captured HTML responses. Modifying them invalidates the scraper tests' contract with reality. If the upstream site changes, recapture rather than hand-edit.

## Off-Limits

- **`tasks/todo .md`** — the locked design doc from the grilling session. Only edit when explicitly asked, and only to record new locked decisions or close out phase boxes.
- **`tests/fixtures/`** — see Gotchas above. Treat as immutable.
- **`data/`** — gitignored runtime state. Don't read the SQLite DB or snapshot files during analysis; they're machine-generated and noise.

## Tools

- **Package operations:** `uv` only. `uv add <pkg>`, `uv remove <pkg>`, `uv sync`, `uv lock`.
- **Logging:** stdlib `logging` with module-level `logger = logging.getLogger(__name__)`. Log level via `LOG_LEVEL` env var.
- **Async style:** all I/O is async (httpx + python-telegram-bot v22). New I/O code must follow the async pattern, not introduce sync I/O.
