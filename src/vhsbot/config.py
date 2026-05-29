"""Environment-driven configuration. Validated once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    scan_time: time
    tz: ZoneInfo
    db_path: Path
    snapshot_dir: Path
    log_level: str
    scrape_sleep_seconds: float

    user_agent: str = (
        "vhs-berlin-bot/0.1 "
        "(+https://github.com/rubenkarlsson/vhs-berlin-bot, "
        "contact: neburgordon@gmail.com)"
    )
    search_url: str = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx"
    results_url: str = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseList.aspx"
    detail_url_template: str = (
        "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseDetail.aspx?id={kurs_id}"
    )


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as e:
            raise RuntimeError(f"ALLOWED_USER_IDS entry is not an int: {part!r}") from e
    if not ids:
        raise RuntimeError("ALLOWED_USER_IDS must contain at least one Telegram user id")
    return frozenset(ids)


def _parse_time(raw: str) -> time:
    try:
        hour_str, minute_str = raw.strip().split(":", 1)
        return time(hour=int(hour_str), minute=int(minute_str))
    except (ValueError, AttributeError) as e:
        raise RuntimeError(f"SCAN_TIME must be HH:MM, got: {raw!r}") from e


def load_settings() -> Settings:
    load_dotenv(override=False)

    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        allowed_user_ids=_parse_user_ids(_require("ALLOWED_USER_IDS")),
        scan_time=_parse_time(os.environ.get("SCAN_TIME", "08:00")),
        tz=ZoneInfo(os.environ.get("TZ", "Europe/Berlin")),
        db_path=Path(os.environ.get("DB_PATH", "/data/vhsbot.db")),
        snapshot_dir=Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        scrape_sleep_seconds=float(os.environ.get("SCRAPE_SLEEP_SECONDS", "2")),
    )
