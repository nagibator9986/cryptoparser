from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from crypto_monitor.models import RawArticle

DEFAULT_TIMEZONE = "Asia/Almaty"

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_KAZAKH_RE = re.compile(r"[ӘәІіҢңҒғҮүҰұҚқӨөҺһ]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def zoneinfo_or_utc(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def normalize_datetime(
    value: datetime | None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zoneinfo_or_utc(timezone_name))


def parse_datetime(value: object, timezone_name: str = DEFAULT_TIMEZONE) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return normalize_datetime(value, timezone_name)
    text = str(value).strip()
    if not text:
        return None
    try:
        return normalize_datetime(parsedate_to_datetime(text), timezone_name)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return normalize_datetime(parsed, timezone_name)
    except ValueError:
        return None


def detect_language(text: str) -> str | None:
    compact = text.strip()
    if not compact:
        return None
    if _KAZAKH_RE.search(compact):
        return "kk"
    cyrillic = len(_CYRILLIC_RE.findall(compact))
    latin = len(_LATIN_RE.findall(compact))
    if cyrillic >= 8 and cyrillic >= latin:
        return "ru"
    if latin >= 20 and latin > cyrillic:
        return "en"
    return None


def normalize_raw_article(
    article: RawArticle,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> RawArticle:
    payload = article.model_dump()
    payload["published_at"] = normalize_datetime(article.published_at, timezone_name)
    if not article.language:
        payload["language"] = detect_language(f"{article.title}\n{article.body}")
    return RawArticle.model_validate(payload)


def digest_date_or_previous_day(
    digest_date: str | None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> str:
    if digest_date:
        return digest_date
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local_date = current.astimezone(zoneinfo_or_utc(timezone_name)).date()
    return (local_date - timedelta(days=1)).isoformat()


def day_bounds(
    day: str,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> tuple[datetime, datetime]:
    local_tz = zoneinfo_or_utc(timezone_name)
    parsed = date.fromisoformat(day)
    start = datetime.combine(parsed, time.min, tzinfo=local_tz)
    return start, start + timedelta(days=1)


def is_datetime_in_day(
    value: datetime | None,
    day: str,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> bool:
    if value is None:
        return False
    local_value = normalize_datetime(value, timezone_name)
    if local_value is None:
        return False
    start, end = day_bounds(day, timezone_name)
    return start <= local_value < end


def is_within_schedule_window(
    now: datetime,
    scheduled_time: str,
    timezone_name: str = DEFAULT_TIMEZONE,
    window_minutes: int = 5,
) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(zoneinfo_or_utc(timezone_name))
    hour_raw, minute_raw = scheduled_time.split(":", 1)
    scheduled = local_now.replace(
        hour=int(hour_raw),
        minute=int(minute_raw),
        second=0,
        microsecond=0,
    )
    return scheduled <= local_now <= scheduled + timedelta(minutes=window_minutes)


def format_article_date(
    value: datetime | None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    normalized = normalize_datetime(value, timezone_name)
    return normalized.strftime("%d.%m.%Y %H:%M") if normalized else "дата не указана"


def http_response_metadata(
    response: httpx.Response,
    max_text_chars: int = 20_000,
) -> dict[str, object]:
    text = response.text
    return {
        "status_code": response.status_code,
        "final_url": str(response.url),
        "content_type": response.headers.get("content-type"),
        "content_length": response.headers.get("content-length"),
        "response_size_chars": len(text),
        "response_excerpt": text[:max_text_chars],
    }
