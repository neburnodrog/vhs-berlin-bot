# vhs-berlin-bot

A private Telegram bot that watches [Berliner Volkshochschulen](https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx) for new bookable courses matching keywords you care about (e.g. `Keramik`, `Yoga`, `Spanisch`).

Runs once each morning at 08:00 Europe/Berlin, scrapes the catalogue scoped to the districts you chose, and sends one Telegram message per match. Also fires when a previously-full course becomes bookable again (cancellations).

## Status

🚧 In development — see [`tasks/todo .md`](tasks/todo%20.md) for the plan.

## Design

- **Single-user, whitelist-based.** Only Telegram user IDs in `ALLOWED_USER_IDS` can interact.
- **District-restricted full crawl.** Each user picks which Berlin districts they care about; the daily job unions everyone's districts and crawls once.
- **Local keyword matching.** Substring, case-insensitive, accent-folded, against the course title and course-number.
- **Per-course notifications.** One message per match (back-in-stock OR newly-listed-and-bookable), capped at 15/day/user.

See [`tasks/todo .md`](tasks/todo%20.md) for the full design decisions and recon facts about the target site.

## Quick start (local)

Requires [uv](https://github.com/astral-sh/uv).

```sh
git clone git@github.com:rubenkarlsson/vhs-berlin-bot.git
cd vhs-berlin-bot
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS in .env
uv sync
uv run vhsbot
```

## Deploy (Coolify on Hetzner)

1. Push to private GitHub repo `vhs-berlin-bot`.
2. New service in Coolify, source = this repo, Dockerfile build.
3. Set env vars from `.env.example` (use Coolify's secret-input for `TELEGRAM_BOT_TOKEN`).
4. Mount a persistent volume at `/data` so SQLite + snapshots survive deploys.
5. Deploy. Bot uses `run_polling()` — no inbound port to expose.

## Env vars

| Var | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather. Required. |
| `ALLOWED_USER_IDS` | — | Comma-separated numeric Telegram user IDs. Required. |
| `SCAN_TIME` | `08:00` | Daily scan time, HH:MM in `TZ`. |
| `TZ` | `Europe/Berlin` | IANA timezone string. |
| `DB_PATH` | `/data/vhsbot.db` | SQLite location. |
| `SNAPSHOT_DIR` | `/data/snapshots` | Raw scraped HTML, pruned weekly. |
| `LOG_LEVEL` | `INFO` | Standard stdlib logging levels. |
| `SCRAPE_SLEEP_SECONDS` | `2` | Sleep between paginated POSTs to vhsit.berlin.de. |

## Politeness

The scraper identifies as `vhs-berlin-bot/0.1 (+https://github.com/rubenkarlsson/vhs-berlin-bot, contact: neburgordon@gmail.com)` and sleeps 2 seconds between paginated POSTs. It honours `robots.txt` (which only excludes `msnbot/1.0` and `Wdb-Suchportal-Bot`). One scrape session per morning, ~10–15 minutes of activity.

If you're from vhsit.berlin.de and want this stopped or rate-limited differently, email the address above.

## License

MIT — see [LICENSE](LICENSE).
