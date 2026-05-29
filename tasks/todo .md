# vhs-berlin-bot — implementation plan

## Locked design (from grilling session 2026-05-29)

| Decision | Choice | Rationale |
|---|---|---|
| User model | (A) **Single-user whitelist** — hardcoded `ALLOWED_USER_IDS` env var | Niche use case; simpler data model; no abuse vector |
| Hosting | (A) **Hetzner via Coolify** (157.180.38.140) | Already owned, always-on, Docker-managed, free marginal cost |
| Discovery strategy | (Y) **Full crawl + local filter**, scoped to user-chosen districts | One scrape answers many subscriptions; clean diff model |
| Notification policy | **Strict + back-in-stock**: new+bookable OR full→bookable transitions; **backfill on `/watch`**; waitlist OFF by default | Catches cancellations (the high-value case) without spam |
| Bot UX | (B) **Conversational onboarding** (`/start` → districts inline keyboard → keyword text), **slash commands** for management | Honors user's "asks the user" brief; warmer first contact |
| Crawl scope | (B) **District-restricted**, user-defined; union all whitelist users' districts | Polite + relevant; first-class concept in VHS Berlin's own UI |
| Matching | Free text, title + course-number prefix, **substring + case-insensitive + Unicode-folded**, OR across keywords, dedup across keywords | Matches user mental model; robust to German compound nouns |
| Notification format | (A) **One message per course**, cap 15/day/user, Markdown V2 with inline "open detail page" button | Each course is independently actionable; cap prevents storms |
| Tech stack | **Python 3.13**, **uv**, **ruff**, **pytest** + pytest-asyncio, **python-telegram-bot v22** with built-in `JobQueue` | Modern Python 2026 default; one library covers handlers + scheduling |
| HTTP | **httpx async client** with session cookies; `User-Agent: vhs-berlin-bot/0.1 (+repo URL, contact: neburgordon@gmail.com)`; 2s sleep between paginated POSTs | Modern async, polite, honest |
| Storage | **SQLite** at `/data/vhsbot.db` (mounted Coolify volume); raw snapshot to `/data/last_snapshot.json` for replay, pruned weekly | Boring, correct, zero ops |
| Repo | Private GitHub repo `vhs-berlin-bot` under personal account (neburgordon@gmail.com identity) | Personal-project boundary [[feedback-personal-email]] |

## Recon facts to encode

- ASP.NET WebForms; form action `./CourseSearch.aspx`, POST with `__VIEWSTATE`, `__VIEWSTATEGENERATOR=2B79C7F0`, `__EVENTVALIDATION`, `__EVENTTARGET=ctl00$Content$btnSearch`, `__EVENTARGUMENT=""`.
- Encoding: **windows-1252** on response (not UTF-8).
- Session timeout: **29.5 min** — complete each scan well within.
- Detail URL: `https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseDetails.aspx?Kurs-ID=<int>`.
- District field: `ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$<N>` checkboxes; values 31–42, 81, 98.
- Search-term field (Erweitert tab — preferred since we need district + search): `ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm` (we send empty to fetch all matching the district filter).
- Pagination via `__doPostBack(eventTarget, eventArgument)` — 10 per page.
- Availability literals in results: `>2`, `2`, `1`, `belegt`. Waitlist behavior is implicit; not surfaced in row text by default.
- `robots.txt` blocks only `msnbot/1.0` and `Wdb-Suchportal-Bot` — generic UA fine.
- Page meta is `nofollow` (soft). No noindex.

## Implementation checklist

### Phase 0 — repo + scaffold ✅ done (except first commit)

- [x] Plan written to `tasks/todo .md`
- [x] Directory skeleton (`src/vhsbot/`, `tests/fixtures/`, `tasks/`)
- [x] `pyproject.toml` — Python 3.13, deps, uv config, ruff config, pytest config
- [x] `.gitignore` — Python + uv + `.env` + `/data/` + `.venv/`
- [x] `.python-version` → `3.13`
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

- [ ] `src/vhsbot/config.py` — Pydantic Settings or stdlib dataclass; load `.env` via `python-dotenv`; validate `ALLOWED_USER_IDS` is non-empty, `TELEGRAM_BOT_TOKEN` is set, `SCAN_TIME` parses as HH:MM
- [ ] `src/vhsbot/db.py` — sqlite3 (sync); idempotent `init_schema()`; CRUD for `subscriptions` and `seen_courses`; one global module-level connection guarded by an `asyncio.Lock` (sqlite3 itself is fine for our concurrency)
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

- [ ] `src/vhsbot/scraper.py`:
  - `async def fetch_form() -> FormState` — GET `CourseSearch.aspx`, extract `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`, capture session cookie
  - `async def search_district(state, district_id: int) -> AsyncIterator[Course]` — POST with the Erweitert tab, district checkbox, empty search term, follow pagination via `__doPostBack` until no "next page" button
  - `async def crawl(districts: set[int]) -> list[Course]` — sequential district loop, 2s sleep between requests
- [ ] `Course` dataclass: `kurs_id, course_number, title, district, venue, date_range, availability, raw_html_hash`
- [ ] Decode response bytes with `response.content.decode('windows-1252')`
- [ ] Parser uses `BeautifulSoup(html, 'lxml')`; extract from `table.CourseListDataGrid` rows
- [ ] Persist raw response HTML per page to `/data/snapshots/YYYY-MM-DD/<district>-page-<N>.html` for debugging (auto-prune >7 days)

### Phase 3 — matching + diff

- [ ] `src/vhsbot/matching.py`:
  - `fold(s: str) -> str` — Unicode NFKD + strip combining + casefold
  - `matches(course: Course, keywords: list[str]) -> list[str]` — return all matched keywords (substring match against folded title + folded course_number)
- [ ] `src/vhsbot/diff.py` (or inside jobs.py):
  - `classify(course, prev_seen) -> "new" | "back_in_stock" | "unchanged" | "still_full"`
  - "back_in_stock": prev availability was `"belegt"` AND current is one of `">2" | "2" | "1"`
  - "new": kurs_id not in seen_courses

### Phase 4 — handlers + onboarding

- [ ] `src/vhsbot/handlers.py`:
  - `start(update, context)` — whitelist check; if known user, greet warmly + show current subs; if new user, kick off `ConversationHandler`
  - District inline keyboard (multi-select via callback_query, 4 cols × 3 rows + "Alle" + "Fertig" buttons)
  - After districts confirmed: prompt for keyword text
  - Save subscription + run on-demand backfill (call `scraper.crawl(districts)` filtered to user's brand-new keyword, send up to 15 bookable matches as individual messages)
  - `/list`, `/watch <kw>`, `/unwatch <kw>`, `/districts`, `/pause`, `/resume`, `/scan` (admin-only manual trigger), `/help`
- [ ] Whitelist middleware: reject any update from user_id not in `ALLOWED_USER_IDS` with a polite "this is a private bot" message

### Phase 5 — daily job

- [ ] `src/vhsbot/jobs.py`:
  - `daily_scan(context)` — union all users' districts; `scraper.crawl()`; classify each course; for each (user, course) pair where the course matches a user's keyword AND classification is `new` or `back_in_stock` (subject to user's `paused` and `include_waitlist` flags), send a notification (cap 15/user/day)
  - Update `seen_courses` with latest availability snapshot
  - Snapshot pruning (delete `/data/snapshots/*` older than 7 days)
- [ ] `src/vhsbot/formatting.py`:
  - `course_card(course, matched_kws, reason) -> tuple[str, InlineKeyboardMarkup]` — Markdown V2 with `escape_markdown(...)`
- [ ] `src/vhsbot/main.py`:
  - Build `Application`, wire handlers, register `ConversationHandler`, register `job_queue.run_daily(daily_scan, time=SCAN_TIME, tzinfo=Europe/Berlin)`
  - Run `application.run_polling()` (no webhook in v1)

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
- [ ] Watch a keyword that should match → confirm backfill sends matches
- [ ] Manually trigger `/scan` → confirm a fresh full crawl runs and stores snapshot
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
