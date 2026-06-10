from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from crypto_monitor.models import RawArticle
from crypto_monitor.normalization import (
    detect_language,
    digest_date_or_previous_day,
    is_datetime_in_window,
    is_within_schedule_window,
    normalize_image_url,
    normalize_raw_article,
    parse_datetime,
)


def test_normalize_image_url_rejects_data_uri_and_tracking_pixels() -> None:
    assert normalize_image_url("data:image/png;base64,AAA") is None
    assert normalize_image_url("https://tracking.doubleclick.net/pixel.gif") is None
    assert normalize_image_url("") is None
    assert normalize_image_url(None) is None


def test_normalize_image_url_resolves_relative_against_base() -> None:
    result = normalize_image_url(
        "/img/cover.jpg", base_url="https://example.com/news/article"
    )
    assert result == "https://example.com/img/cover.jpg"


def test_normalize_image_url_promotes_protocol_relative_to_https() -> None:
    assert (
        normalize_image_url("//cdn.example.com/cover.jpg")
        == "https://cdn.example.com/cover.jpg"
    )


def test_detect_language_basic_cases() -> None:
    assert detect_language("Национальный банк сообщил о цифровом тенге") == "ru"
    assert detect_language("Kazakhstan regulator approved a crypto license for a bank") == "en"
    assert detect_language("Ұлттық банк цифрлық теңге туралы хабарлады") == "kk"


def test_parse_datetime_normalizes_to_almaty() -> None:
    parsed = parse_datetime("2026-05-26T23:30:00Z")
    assert parsed is not None
    assert parsed.tzinfo == ZoneInfo("Asia/Almaty")
    assert parsed.date().isoformat() == "2026-05-27"


def test_default_digest_date_is_previous_local_day() -> None:
    now = datetime(2026, 5, 27, 4, 0, tzinfo=UTC)
    assert digest_date_or_previous_day(None, now=now) == "2026-05-26"


def test_schedule_window_accepts_delivery_slack() -> None:
    now = datetime(2026, 5, 27, 4, 4, tzinfo=UTC)
    assert is_within_schedule_window(now, "09:00", "Asia/Almaty")
    late = datetime(2026, 5, 27, 4, 6, tzinfo=UTC)
    assert not is_within_schedule_window(late, "09:00", "Asia/Almaty")


def test_window_lookback_widens_backwards() -> None:
    # 2026-06-01 13:00 Almaty == 2026-06-01 08:00 UTC.
    two_days_before = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    now = datetime(2026, 6, 20, 0, 0, tzinfo=UTC)  # well past the window; no extension
    # Strict single day excludes it; lookback=3 includes it.
    assert not is_datetime_in_window(two_days_before, "2026-06-03", 1, "Asia/Almaty", now=now)
    assert is_datetime_in_window(two_days_before, "2026-06-03", 3, "Asia/Almaty", now=now)
    # Outside the window even with lookback.
    assert not is_datetime_in_window(two_days_before, "2026-06-03", 2, "Asia/Almaty", now=now)


def test_window_includes_today_stamped_item_for_live_previous_day_digest() -> None:
    # Morning digest labelled for "yesterday" must still include this morning's
    # fresh scrape: dateless html/gov.kz items get a collection-time (today) stamp.
    now = datetime(2026, 6, 10, 4, 0, tzinfo=UTC)  # 09:00 Asia/Almaty
    today_item = datetime(2026, 6, 10, 3, 30, tzinfo=UTC)
    assert is_datetime_in_window(today_item, "2026-06-09", 1, "Asia/Almaty", now=now)
    # Historical rebuild stays bounded: a now-stamped item is NOT pulled into a
    # months-old digest.
    assert not is_datetime_in_window(today_item, "2026-01-01", 1, "Asia/Almaty", now=now)


def test_kazakh_overrides_wrong_language_hint() -> None:
    # KZ feeds tag everything ru; Kazakh letters must win.
    article = RawArticle(
        id="1",
        source_id="kapital-kz",
        source_name="Kapital",
        source_url="https://kapital.kz/a",
        title="Түркістан облысында агродрондар өндірісі іске қосылды",
        body="Агродрондар ауыл шаруашылығы дақылдарын бүрку үшін.",
        language="ru",
    )
    assert normalize_raw_article(article).language == "kk"
