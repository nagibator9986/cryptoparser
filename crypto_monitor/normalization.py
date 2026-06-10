from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from crypto_monitor.models import RawArticle

DEFAULT_TIMEZONE = "Asia/Almaty"

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_KAZAKH_RE = re.compile(r"[ӘәІіҢңҒғҮүҰұҚқӨөҺһ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_IMAGE_PATH_RE = re.compile(r"\.(jpe?g|png|gif|webp|svg|avif)(\?|$)", re.IGNORECASE)
_TRACKING_PIXEL_HINTS = (
    "1x1",
    "pixel",
    "doubleclick.net",
    "googletagmanager.com",
    "facebook.com/tr",
    "google-analytics.com",
    "/beacon",
)


def normalize_image_url(value: object, base_url: str | None = None) -> str | None:
    """Validate and absolutize an image URL extracted from RSS or HTML.

    Returns None for empty input, tracking pixels, data: URIs (Telegram does
    not accept them), and protocol-relative URLs lacking a base. Resolves
    relative paths against ``base_url`` when provided.
    """

    if not value:
        return None
    text = str(value).strip()
    if not text or text.startswith("data:"):
        return None
    if base_url and not text.startswith(("http://", "https://", "//")):
        text = urljoin(base_url, text)
    if text.startswith("//"):
        text = "https:" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    lowered = text.lower()
    if any(hint in lowered for hint in _TRACKING_PIXEL_HINTS):
        return None
    return text


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
    detected = detect_language(f"{article.title}\n{article.body}")
    # Kazakh letters are unambiguous, so trust detection over a (frequently
    # wrong) source `language_hint` — KZ media feeds tag everything "ru".
    # Otherwise only fill the language when the hint is absent.
    if detected == "kk":
        payload["language"] = "kk"
    elif not article.language:
        payload["language"] = detected
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


def is_datetime_in_window(
    value: datetime | None,
    day: str,
    lookback_days: int = 1,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> bool:
    """Whether ``value`` falls in the digest window anchored on ``day``.

    ``lookback_days=1`` reproduces the strict single-day filter (the ТЗ
    "за предыдущие сутки" default). Larger values widen the window backwards so
    low-frequency sources (KZ/CIS regulators that do not publish every day)
    still surface in a daily digest.

    For a *live* digest (``day`` is today or within ``lookback_days`` of it) the
    window end is extended to ``now``. This is what makes this morning's fresh
    scrape land in the digest: HTML/gov.kz items without a parseable publish
    date are stamped with the collection time, which is "today" even when the
    digest is labelled for the previous day. Historical rebuilds (``day`` far in
    the past) stay bounded to the end of ``day``.
    """

    if value is None:
        return False
    local_value = normalize_datetime(value, timezone_name)
    if local_value is None:
        return False
    start, end = day_bounds(day, timezone_name)
    start = start - timedelta(days=max(0, lookback_days - 1))
    current = normalize_datetime(now or datetime.now(UTC), timezone_name)
    if current is not None and end <= current <= end + timedelta(days=max(1, lookback_days)):
        end = current + timedelta(seconds=1)
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


def http_response_metadata(response: httpx.Response) -> dict[str, object]:
    # Audit metadata is duplicated into every article's stored payload, so it
    # must stay small. Storing the full response body here previously bloated
    # the SQLite payload by the size of the whole feed/page per article.
    return {
        "status_code": response.status_code,
        "final_url": str(response.url),
        "content_type": response.headers.get("content-type"),
        "content_length": response.headers.get("content-length"),
        "response_size_bytes": len(response.content),
    }
