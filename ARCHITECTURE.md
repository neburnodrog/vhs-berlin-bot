# Architecture

> Single-user Telegram bot with a daily ASP.NET-scraping pipeline. Layered async Python; no framework beyond `python-telegram-bot v22`.

## Overview

The bot runs a single long-lived process driven by python-telegram-bot's polling loop. A `JobQueue` fires `daily_scan` once a day at the configured local time. That job unions all active users' subscribed Berlin districts, drives one HTTP-scraping session per district through the VHS Berlin ASP.NET WebForms catalog, diffs each course against SQLite-persisted prior state, fans matches out to the subscribing users, and prunes old snapshots. The same `daily_scan` callback also backs the on-demand `/scan` command — gated by a `scan_running` `bot_data` flag so manual and scheduled triggers never overlap.

The architecture is deliberately small (12 source modules, ~2,400 LOC). Heavy lifting lives in pure-logic modules (`parser`, `matching`, `diff`, `formatting`) that take dataclasses and return dataclasses; the I/O-touching modules (`scraper`, `db`, `handlers`, `jobs`) compose them. `main.py` is the only place that wires resources together.

## Module Map

| Module | Path | Purpose | Key Files |
|---|---|---|---|
| Bootstrap | `src/vhsbot/main.py` | Builds the PTB `Application`, opens shared resources, registers handlers + the daily job, runs polling. | `main.py` |
| App state | `src/vhsbot/_app_state.py` | `BD_*` string-constant keys for `application.bot_data` + `locked_db` async context manager. Shared by `main`, `handlers`, `jobs`. | `_app_state.py` |
| Config | `src/vhsbot/config.py` | Env-driven `Settings` dataclass (frozen, slots). Validated once at startup. | `config.py` |
| Storage | `src/vhsbot/db.py` | SQLite schema + CRUD. Every function takes an explicit `sqlite3.Connection`. | `db.py` |
| Scraper | `src/vhsbot/scraper.py` | HTTP orchestrator for the VHS ASP.NET search flow (GET form → POST Erweitert → POST search → paginate). | `scraper.py` |
| Parser | `src/vhsbot/parser.py` | BeautifulSoup-driven extraction of form state, district map, and course rows. Pure. | `parser.py` |
| Matching | `src/vhsbot/matching.py` | Substring/Unicode-folded keyword matching against course titles + course-numbers. Pure. | `matching.py` |
| Diff | `src/vhsbot/diff.py` | Classifies a `CourseSnapshot` against prior `SeenCourse` into `new` / `back_in_stock` / `unchanged` / `still_full`. Pure. | `diff.py` |
| Formatting | `src/vhsbot/formatting.py` | Renders Telegram MarkdownV2 messages with the "open detail page" inline button. Pure. | `formatting.py` |
| Handlers | `src/vhsbot/handlers.py` | Telegram command + conversation handlers (`/start`, `/watch`, `/unwatch`, `/list`, `/districts`, `/pause`, `/resume`, `/scan`). | `handlers.py` |
| Jobs | `src/vhsbot/jobs.py` | The `daily_scan` `JobQueue` callback — pure orchestration over the modules above. | `jobs.py` |

## Entry Points

- **Process entry**: `src/vhsbot/main.py::run` — script entry point declared in `pyproject.toml` `[project.scripts]` as `vhsbot`. Loads `Settings`, builds the `Application`, calls `run_polling()`.
- **Testable surface**: `src/vhsbot/main.py::build_application` — assembles the `Application` with all handlers + the daily-scan job registered but does NOT call `run_polling`. Tests use this to introspect `app.job_queue.jobs()`, `app.error_handlers`, and `app.handlers` without opening a Telegram connection.
- **Daily-scan callback**: `src/vhsbot/jobs.py::daily_scan` — registered both by `build_application` (scheduled trigger) and by the `/scan` handler (manual trigger).
- **DB schema**: inline string `_SCHEMA` in `src/vhsbot/db.py` (4 tables: `subscriptions`, `user_settings`, `seen_courses`, `notification_log`).
- **Handler registration**: `src/vhsbot/handlers.py::register_handlers` — the single function `main.py` calls to wire every Telegram handler.

## Invariants

- **No globals; no scattered string keys.** All shared resources (`Settings`, `sqlite3.Connection`, `asyncio.Lock`, `httpx.AsyncClient`) live on `application.bot_data` and are looked up via the `BD_*` constants in `_app_state.py`. Adding a new shared resource means adding a constant there first.
- **DB writes serialised through `locked_db()`.** Every site that touches sqlite goes through `_app_state.locked_db(context)`. The lock is shallow — it does NOT cover the network awaits in callers — but it serialises the writes themselves. Bypassing this risks `sqlite3.OperationalError: database is locked` under handler contention.
- **`db.py` takes an explicit `Connection`.** No module-level singleton. This is what lets tests use `:memory:` connections with full isolation per test.
- **`build_application` must stay separable from `run`.** A typo in the daily-scan callback name or schedule time must be caught by a unit test, not in production. Do not collapse them.
- **One `httpx.AsyncClient` per scan session.** Cookie continuity is required across the multi-POST VHS flow; per-request clients break the 29.5-min session.
- **`AVAILABILITY_LITERALS` is the contract between parser and diff.** `diff.classify()` defensively validates both current and previous literals — symmetric guard so parser drift OR stored-state drift surfaces at classification time rather than silently mis-classifying. Adding a new literal means updating `AVAILABILITY_LITERALS` (in `db.py`) AND the matcher/classifier sites that reference it.
- **`jobs.daily_scan` drives `scraper.crawl_district` directly.** It does NOT call `scraper.crawl`'s all-or-nothing wrapper. This is deliberate: a single failing district must leave the other districts' snapshots persisted. The wrapper exists for the synchronous `/watch` backfill where atomicity matters.
- **Cross-day 15/user cap uses the prior-count snapshot, not a re-query.** `daily_scan` snapshots each user's prior-24h `notification_log` count ONCE at scan start. Re-querying inside the fan-out loop would double-count (each send both inserts a log row AND bumps the in-scan counter, halving the budget).

## Cross-Cutting Concerns

| Concern | Implementation | Location |
|---|---|---|
| Authentication | `ALLOWED_USER_IDS` whitelist (env var), enforced as the first check in every handler | `src/vhsbot/handlers.py` |
| Concurrency | One async DB lock + PTB's `AIORateLimiter` for outbound sends | `_app_state.locked_db`, `main.py::build_application` |
| Logging | stdlib `logging` with module-level `logger = logging.getLogger(__name__)`. Level from `LOG_LEVEL` env var | every module |
| Error handling | PTB's `add_error_handler` catches handler + job exceptions; `jobs.daily_scan` re-raises after logging | `handlers.py`, `jobs.py` |
| Politeness | Custom `User-Agent` in `Settings` + 2s sleep between paginated POSTs | `config.py::Settings.user_agent`, `scraper.py` |
| Persistence of debug state | Raw HTML snapshots written to `$SNAPSHOT_DIR/YYYY-MM-DD/<district>-page-<N>.html` via callback into `scraper.crawl_district`; pruned after 7 days | `jobs.py` |

## External Integrations

| System | Protocol | Purpose | Config |
|---|---|---|---|
| Telegram Bot API | `python-telegram-bot v22` long-polling | Inbound commands + outbound notifications | `TELEGRAM_BOT_TOKEN` env var |
| VHS Berlin catalog | `httpx` async + BeautifulSoup over ASP.NET WebForms | Scrape course listings per district | `Settings.search_url` / `results_url` (no key — public site) |
| SQLite (local file) | stdlib `sqlite3` | Persist subscriptions, seen courses, notification log | `DB_PATH` env var (default `/data/vhsbot.db`) |

## Data Flow

Daily scan (08:00 Europe/Berlin by default):

1. `JobQueue` fires `daily_scan` callback.
2. `jobs.daily_scan` queries `user_settings` for active (non-paused) users and unions their `districts_csv` lists. Empty union → skip the scan.
3. For each district, `scraper.crawl_district` runs the VHS flow (GET form → POST Erweitert tab → POST search → follow 302 → paginate via right-arrow image-input POSTs). Each response HTML is also written to the daily snapshot dir.
4. `parser.parse_results_page` extracts `CourseSnapshot` rows from each page.
5. For each course, `diff.classify` compares against the prior `seen_courses` row to bucket as `new` / `back_in_stock` / `unchanged` / `still_full`.
6. For `new` and `back_in_stock` courses, fan out to every active user whose keywords match (`matching.match`) AND whose subscribed districts include the course's district, capped at 15 sends/user/day (using the start-of-scan prior-count snapshot + in-scan accumulator).
7. `formatting.render` builds the MarkdownV2 message; `context.bot.send_message` (rate-limited by `AIORateLimiter`) delivers it.
8. Every encountered course is upserted into `seen_courses`; notified ones also write a `notification_log` row.
9. After all districts, prune snapshot directories older than 7 days. If any district crawl raised, re-raise the first exception so PTB's `add_error_handler` sees it.

The synchronous `/watch` backfill follows the same pipeline but with the calling user as the only fan-out target and uses `scraper.crawl` (all-or-nothing) rather than per-district orchestration.
