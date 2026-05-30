# vhs-berlin-bot — implementation plan

## Locked design (from grilling session 2026-05-29)

| Decision | Choice | Rationale |
|---|---|---|
| User model | (A) **Single-user whitelist** — hardcoded `ALLOWED_USER_IDS` env var | Niche use case; simpler data model; no abuse vector |
| Hosting | (A) **Hetzner via Coolify** (157.180.38.140) | Already owned, always-on, Docker-managed, free marginal cost |
| Discovery strategy | (Y) **Full crawl + local filter**, scoped to user-chosen districts | One scrape answers many subscriptions; clean diff model |
| Notification policy | **Strict + back-in-stock**: new+bookable OR full->bookable transitions; **backfill on `/watch`**; waitlist OFF by default | Catches cancellations (the high-value case) without spam |
| Bot UX | (B) **Conversational onboarding** (`/start` -> districts inline keyboard -> keyword text), **slash commands** for management | Honors user's "asks the user" brief; warmer first contact |
| Crawl scope | (B) **District-restricted**, user-defined; union all whitelist users' districts | Polite + relevant; first-class concept in VHS Berlin's own UI |
| Matching | Free text, title + course-number prefix, **substring + case-insensitive + Unicode-folded**, OR across keywords, dedup across keywords | Matches user mental model; robust to German compound nouns |
| Notification format | (A) **One message per course**, cap 15/day/user, Markdown V2 with inline "open detail page" button | Each course is independently actionable; cap prevents storms |
| Tech stack | **Python 3.13**, **uv**, **ruff**, **pytest** + pytest-asyncio, **python-telegram-bot v22** with built-in `JobQueue` | Modern Python 2026 default; one library covers handlers + scheduling |
| HTTP | **httpx async client** with session cookies; `User-Agent: vhs-berlin-bot/0.1 (+repo URL, contact: neburgordon@gmail.com)`; 2s sleep between paginated POSTs | Modern async, polite, honest |
| Storage | **SQLite** at `/data/vhsbot.db` (mounted Coolify volume); raw HTML snapshots to `/data/snapshots/YYYY-MM-DD/<district>-page-<N>.html` (one file per result page per district per day), pruned after 7 days | Boring, correct, zero ops |
| Repo | Private GitHub repo `vhs-berlin-bot` under personal account (neburgordon@gmail.com identity) | Personal-project boundary [[feedback-personal-email]] |

## Recon facts to encode (corrected 2026-05-29 from live captures)

The original recon in this section was partially wrong. The corrected flow below is verified against `tests/fixtures/*.html` (real captured responses); see `tests/fixtures/README.md` for the full report.

- ASP.NET WebForms. Two distinct pages in the search flow:
  - `CourseSearch.aspx` — the form. Default tab is *Einfach*; the district checkbox list lives on the *Erweitert* tab.
  - `CourseList.aspx` — the results page. Server **302-redirects** here after a successful search; subsequent pagination POSTs also target this URL.
- Hidden state fields:
  - `__VIEWSTATE`, `__VIEWSTATEGENERATOR` — present on every response. Re-extract from each response before the next POST.
  - `__EVENTVALIDATION` — **absent on the initial GET**, present on `CourseList.aspx` responses.
- **Required flow** (cookies persist across all steps via one `httpx.AsyncClient`):
  1. `GET CourseSearch.aspx` — extract initial state.
  2. `POST CourseSearch.aspx` with `ctl00$Content$lbtnTab2=Erweitert` + state — switches to the advanced tab. Re-extract state from response.
  3. `POST CourseSearch.aspx` with refreshed state +
     - `ctl00$Content$btnSearch=Suchen` (it's a real submit button with `useSubmitBehavior=true` — do **not** use `__EVENTTARGET=...btnSearch`),
     - district checkbox(es): `ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$<N>=on`,
     - empty search term: `ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm=`.
     The server 302s to `CourseList.aspx`; httpx auto-follows.
  4. To paginate: find `<input type="image" name="ctl00$Content$ILDataGrid1$ctl01$ctl04" ...>` in the result HTML (that's the right-arrow). POST to `CourseList.aspx` with refreshed state plus image-submit coords `<name>.x=5&<name>.y=5`. Pagination is **not** `__doPostBack`.
- Encoding: the HTTP `Content-Type` header lies (says `iso-8859-15`); the page's `<meta charset>` is `windows-1252` and that's truthful. Save `response.content` raw, decode with `.decode('windows-1252')`. Umlauts then render correctly.
- Session timeout: **29.5 min** — complete each scan well within.
- Detail URL: `https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseDetail.aspx?id=<int>` (singular *Detail*, lowercase `id` query param — verified from the page-1 fixture).
- District field: `ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$<N>` where N is the *checkbox index*, not the district id. District 31 (Mitte) -> index 5. Build the full N->district-id map by parsing `form-initial.html` once at startup, or hardcode after verification.
- Result rows on `CourseList.aspx`: `<tr class="DataGridItem">` and `<tr class="DataGridAlternatingItem">`; each row contains a link of the shape `CourseDetail.aspx?id=<int>`. 10 results per page. Page label `Seite <N> von <M>` indicates total pages.
- Availability literals in results: `>2`, `2`, `1`, `belegt`. Waitlist behavior is implicit; not surfaced in row text by default.
- `robots.txt` blocks only `msnbot/1.0` and `Wdb-Suchportal-Bot` — generic UA fine.
- Page meta is `nofollow` (soft). No noindex.

## Implementation checklist

### Phase 0 — repo + scaffold done (except first commit)

- [x] Plan written to `tasks/todo .md`
- [x] Directory skeleton (`src/vhsbot/`, `tests/fixtures/`, `tasks/`)
- [x] `pyproject.toml` — Python 3.13, deps, uv config, ruff config, pytest config
- [x] `.gitignore` — Python + uv + `.env` + `/data/` + `.venv/`
- [x] `.python-version` -> `3.13`
- [x] `.env.example` — all env vars with placeholders
- [x] `README.md` — quick start, env vars, deploy notes
- [x] `Dockerfile` — multi-stage uv build on `python:3.13-slim`
- [x] `.dockerignore`
- [x] `src/vhsbot/__init__.py`
- [x] `src/vhsbot/config.py` — env loader with validation
- [x] Git init, set `user.email = neburgordon@gmail.com` locally
- [x] `uv sync` — verified, 22 packages resolved (PTB 22.7, httpx 0.28, lxml 5.4, ruff 0.15)
- [ ] Initial commit (pending user confirmation)
- [ ] Create private GitHub repo (pending user confirmation)

### Phase 1 — config + storage

- [x] `src/vhsbot/config.py` — stdlib `dataclass`; loads `.env` via `python-dotenv`; validates `ALLOWED_USER_IDS` non-empty, `TELEGRAM_BOT_TOKEN` set, `SCAN_TIME` parses as HH:MM
- [x] `src/vhsbot/db.py` — sqlite3 (sync); idempotent `init_schema()`; CRUD for `subscriptions`, `user_settings`, `seen_courses`, `notification_log`. **Connection is passed explicitly** to every function (production wires a single connection at startup with `asyncio.Lock` at the call sites; tests pass `:memory:` per test). 22 tests, all green; ruff clean.
- [ ] Schema:
  ```sql
  CREATE TABLE IF NOT EXISTS subscriptions (
      user_id INTEGER NOT NULL,
      keyword TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (user_id, keyword)
  );
  CREATE TABLE IF NOT EXISTS user_settings (
      user_id INTEGER PRIMARY KEY,
      districts_csv TEXT NOT NULL,           -- e.g. "31,38,39"
      paused INTEGER NOT NULL DEFAULT 0,     -- 0 or 1
      include_waitlist INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
  CREATE TABLE IF NOT EXISTS seen_courses (
      kurs_id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      course_number TEXT NOT NULL,
      district TEXT,
      venue TEXT,
      date_range TEXT,
      last_availability TEXT NOT NULL,        -- ">2" | "2" | "1" | "belegt"
      first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
      last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
      last_notified_at TEXT
  );
  CREATE TABLE IF NOT EXISTS notification_log (
      user_id INTEGER NOT NULL,
      kurs_id INTEGER NOT NULL,
      sent_at TEXT NOT NULL DEFAULT (datetime('now')),
      reason TEXT NOT NULL,                   -- "new" | "back_in_stock" | "backfill"
      PRIMARY KEY (user_id, kurs_id, reason)
  );
  ```

### Phase 2 — scraper

- [x] **Phase 2a — parser** (`src/vhsbot/parser.py`): `parse_form_state`, `parse_results_page`, `has_next_page`. Pure functions over raw response bytes; decoded inside the parser with `windows-1252`. 11 tests.
- [x] **Phase 2b — HTTP orchestrator** (`src/vhsbot/scraper.py`): `crawl_district(client, district_checkbox_index, sleep_seconds)` drives the full GET-form -> POST-Erweitert -> POST-search -> POST-next-page loop. State re-parsed from every response. Sleep between requests configurable. Dependency-injected `httpx.AsyncClient` (production wires UA + cookies; tests inject `_FixtureTransport` replaying captured HTML). 3 tests; verified the orchestrator (a) uses the real `btnSearch=Suchen` submit, not `__EVENTTARGET`, (b) sends district checkbox by index, (c) paginates via image-input coords with refreshed `__EVENTVALIDATION`, (d) stops when `has_next_page` returns false. Total 37 tests, ruff clean.
- [x] **Phase 2c — district map + crawl wrapper**: `parse_district_map(html_bytes)` reads the GET response and yields `district_id -> checkbox_index` (15 entries, anchor 31 -> 5 verified). `crawl(client, district_ids, sleep_seconds)` does one initial GET to build the map, validates ids (raises `ValueError` listing unknown ids), then loops `crawl_district` per district in sorted order and dedups snapshots by `kurs_id` (first-occurrence wins). 3 new tests; 40 total, ruff clean. **Snapshot persistence deferred to Phase 5** — it is an operational concern coupled to the daily scheduler, not the pure HTTP orchestrator.
  - **Review pass (post-0380405)**: `parse_district_map` moved from `scraper.py` to `parser.py` (its natural home — pure-bytes parsing alongside `parse_form_state`/`parse_results_page`). "Alle Bezirke" wildcard (district id 0) now filtered out of the map so callers cannot pass district_id=0 through validation. `crawl` short-circuits to `[]` on empty `district_ids` (no network), widened to `Iterable[int]`. Dedup test rewritten: a new `_DedupTransport` mints per-district disjoint-but-overlapping result sets (ids 1000..1009 vs 1005..1014) with per-district title sentinels, so the test now proves both (a) cross-district dedup and (b) first-occurrence-wins. Plus five new edge-case tests (empty bytes, non-int value, duplicate-district, Alle-Bezirke exclusion, empty district set, single-district crawl). 46 total, ruff clean.

### Phase 3 — matching + diff

- [x] `src/vhsbot/matching.py`:
  - `fold(s: str) -> str` — Unicode NFKD + strip combining + casefold + whitespace collapse. Idempotent. 4 tests pin the umlaut/ß/whitespace/idempotence contract.
  - `matches(course: CourseSnapshot, keywords: Iterable[str]) -> list[str]` — substring match against `fold(title) + " " + fold(course_number)`; skips empty keywords; dedups by folded form; preserves original casing in output. 9 tests.
- [x] `src/vhsbot/diff.py`:
  - `classify(current, previous) -> ClassifyResult` (`Literal["new", "back_in_stock", "unchanged", "still_full"]`, re-exported).
  - "new" when previous is None; "back_in_stock" on belegt->{>2,2,1}; "still_full" on belegt->belegt; "unchanged" otherwise (incl. going-out-of-stock).
  - Raises `ValueError` if `current.availability` is not one of the four parser literals. 18 tests (mostly parametrized).
  - **Review pass (post-4ba1f64)**: availability literals hoisted to `db.py` as single source of truth (`Availability` Literal alias, `AVAILABILITY_LITERALS`, `BOOKABLE_AVAILABILITY`); `parser._availability` and `diff.classify` now import from there. `classify` validates `previous.last_availability` symmetrically (drift in stored state raises just like drift in parser output). Test additions: input-order pin for `matches`, bridge-behavior pin for space-containing keywords (with docstring callout), fold/match edge cases, parser↔db cross-module invariant test. 95 total, ruff clean.

### Phase 4 — handlers + onboarding

- [x] `src/vhsbot/handlers.py`:
  - [x] `start(update, context)` — whitelist check; if known user, greet warmly + show current subs; if new user, kick off `ConversationHandler`
  - [x] District inline keyboard (multi-select via callback_query, **3 cols** × N rows + "Alle" + "Fertig" buttons; the spec wrote 4 cols × 3 rows but the design table says 3 cols by N rows — went with 3 to match the table)
  - [x] After districts confirmed: prompt for keyword text
  - [x] Save subscription + run on-demand backfill (call `scraper.crawl(districts)` filtered to user's brand-new keyword, send up to 15 bookable matches as individual messages, log each to `notification_log` with reason="backfill")
  - [x] `/list`, `/watch <kw>`, `/unwatch <kw>`, `/districts`, `/pause`, `/resume`, `/help` all implemented
  - [x] `/scan` stubbed — sends ack + logs; the actual JobQueue wiring lands in Phase 5 (TODO marker in code)
- [x] Whitelist middleware: `@whitelist_only` decorator wraps every handler; rejects non-whitelisted updates with a polite message and `ConversationHandler.END`
- [x] `src/vhsbot/main.py` bootstrap: `Application.builder().post_init/post_shutdown(...)`, shared `httpx.AsyncClient` + sqlite connection live on `application.bot_data`, `run_polling()`. The Phase 5 daily-scan job will hook onto the existing `Application.job_queue`.
- [x] **Decision pinned**: on-demand backfill blocks the handler (option A). Documented in `src/vhsbot/main.py` module docstring so Phase 5 does not accidentally revisit it.
- **Deferred to Phase 5**: cross-day 15-msg/user cap (would query `notification_log` with a date window), and the actual daily scan loop. Tests: 25 added (18 pure-function + 7 handler-smoke). Total: 120 tests, ruff lint+format clean.
- [x] **Phase 4 review pass (7 reviewers, 2026-05-30)** — fixed: structural `filters.User` whitelist (replaces decorator), `asyncio.Lock`-guarded `_locked_db` helper around every sqlite call, `AIORateLimiter` for outbound throttling, global `add_error_handler`, backfill resilient to crawl failure (subscription preserved), PTB's `escape_markdown(version=2)` instead of hand-rolled escaper, single `query.answer()` per branch in `on_district_toggle`, `/cancel` clears user_data + conversation `fallbacks` route any slash command to a clean exit, friendly "VHS Berlin appears to be down" message on `_fetch_district_map` HTTP failure, hoisted `BD_*` keys into `_app_state.py`, dropped misleading "(admin only)" from `/help` and the dead `all_active_user_ids` re-export. 22 new tests (142 total), ruff clean.

### Phase 5 — daily job

- [x] `src/vhsbot/jobs.py`:
  - [x] `daily_scan(context)` — union all users' districts; `scraper.crawl()`; classify each course; for each (user, course) pair where the course matches a user's keyword AND classification is `new` or `back_in_stock` (subject to user's `paused` and `include_waitlist` flags), send a notification (cap 15/user/day, cross-day window = trailing 24h)
  - [x] Update `seen_courses` with latest availability snapshot via new `upsert_seen_course(conn, snapshot, notified=...)` — every returned course is upserted, with `last_notified_at` only bumped when notified=True
  - [x] Persist raw response HTML to `<snapshot_dir>/YYYY-MM-DD/<district>-page-<N>.html` via the new `raw_html_callback` parameter on `scraper.crawl` / `scraper.crawl_district` (Phase 2c "deferred to Phase 5" — done here)
  - [x] Snapshot pruning (delete `<snapshot_dir>/<YYYY-MM-DD>` dirs older than 7 days; non-date-named dirs left alone)
  - [x] Top-level try/except re-raises so PTB's global error handler sees the failure
- [x] `src/vhsbot/formatting.py`:
  - [x] `course_card(course, matched_kws, reason, detail_url) -> tuple[str, InlineKeyboardMarkup]` — Markdown V2 via PTB's `escape_markdown(version=2)`. `reason ∈ {"new","back_in_stock","backfill"}` drives a prefix line; the old `handlers.build_course_message` is deleted and `_run_backfill` now calls `course_card(..., reason="backfill")`.
- [x] `src/vhsbot/main.py`:
  - [x] `job_queue.run_daily(daily_scan, time=settings.scan_time.replace(tzinfo=settings.tz), name="vhsbot-daily-scan")` registered after `register_handlers`.
- **Tests added in Phase 5**: 4 `test_formatting.py` (reason prefix x3 + MD-V2 escape + button URL), 9 `test_jobs.py` (skip empty / classify / fanout / paused / cap / unconditional upsert / snapshot persistence / pruning + exception re-raise), 2 `test_scraper.py` (callback invocation per page + backward-compat default-None). Plus 3 in `test_db.py` for `upsert_seen_course`. 142 -> 157 tests, ruff clean.
- [x] **Phase 5 review pass (7 reviewers, 2026-05-30)** — BLOCKER: cap-counter no longer double-counts in-scan sends (`prior_count` snapshotted once per user at scan start). MAJORs fixed: per-user district filtering in the fan-out (was unioned and over-broadcasting), `/scan` wired to `daily_scan` with a `scan_running` concurrency guard, partial-district failure recovery (`daily_scan` now drives `crawl_district` per-district + try/except so a single failing district doesn't strand the others' seen_courses upserts; first error re-raised after the rest of the scan persists), main.py wiring coverage via extracted `build_application` helper. `add_error_handler` was already registered in `handlers.register_handlers` (line 671) — reviewer's grep was wrong; now also pinned by `test_main.py`. MINORs bundled: `_UserSubs` -> frozen dataclass with `keywords: tuple[str, ...]` and `include_waitlist` dropped (inert), `_locked_db` hoisted to `_app_state.py` as `locked_db` (shared between handlers + jobs), pruning boundary tightened (`<= cutoff` so exactly-7-day-old dirs are pruned), snapshot writer + prune wrapped in `try/except OSError` + warning log (debug-only artefacts never abort the scan), `shutil.rmtree` replaces the hand-rolled `_rmtree`, dead `_utc_now_iso` deleted, dead `__all__ = ["CourseSnapshot", ...]` re-export dropped, `_since_24h_iso` computed once at scan start, `Any` types replaced (`sqlite3.Connection` + `scraper.RawHtmlCallback`), `NotificationReason` literal added in `diff.py` and consumed via `cast` in the fan-out, runtime `assert` added to `course_card` for invalid reasons. 157 -> 169 tests, ruff clean.

### Phase 6 — tests

- [ ] Capture real HTML fixtures: a form-state GET, one search-result page (district=31, empty search), one paginated result page
- [ ] `tests/test_scraper.py` — parser unit tests against fixtures (golden-file pattern)
- [ ] `tests/test_matching.py` — fold/match correctness across umlaut + casing + substring edge cases
- [ ] `tests/test_diff.py` — classification table-driven tests for all 5 cases from Q4
- [ ] `tests/test_handlers.py` — minimal smoke test with `pytest-asyncio` + mock Update/Context

### Phase 7 — deploy

- [ ] Dockerfile build locally: `docker build -t vhs-berlin-bot:dev . && docker run --rm -e TELEGRAM_BOT_TOKEN=... -e ALLOWED_USER_IDS=... -v /tmp/vhsdata:/data vhs-berlin-bot:dev`
- [ ] Create Telegram bot via @BotFather, get token, set bot description + commands list
- [ ] Get own Telegram user_id (via @userinfobot)
- [ ] Push private repo to GitHub
- [ ] In Coolify: new service from the repo, env vars (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, TZ=Europe/Berlin, DB_PATH=/data/vhsbot.db, SCAN_TIME=08:00), volume mount `/data`
- [ ] Deploy; verify logs show "ready"; send `/start` to the bot from Telegram

### Phase 8 — verification (per CLAUDE.md "Verification Before Done")

- [ ] `/start` flow works end-to-end on real Telegram client
- [ ] Watch a keyword that should match -> confirm backfill sends matches
- [ ] Manually trigger `/scan` -> confirm a fresh full crawl runs and stores snapshot
- [ ] Simulate "back in stock": manually flip a `seen_courses.last_availability` from "belegt" to ">2" via sqlite3 CLI; trigger /scan; confirm notification fires
- [ ] Confirm 08:00 schedule by setting SCAN_TIME to "T+1min" temporarily and watching logs
- [ ] Restart container; confirm subscriptions persist (DB volume works)

## Out of scope for v1

- Stichwort dropdown selection (free-text keywords only)
- Course description / detail-page parsing (title-only matching)
- Web dashboard
- iCal / calendar export
- Course-cancellation alerts (only "back in stock" the reverse direction)
- Multi-language UX (DE-only messages)
- Webhook deployment (use `run_polling()` — fine for one container)

## Review section

_To be filled after implementation._
