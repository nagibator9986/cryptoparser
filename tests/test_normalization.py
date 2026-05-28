from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from crypto_monitor.normalization import (
    detect_language,
    digest_date_or_previous_day,
    is_within_schedule_window,
    parse_datetime,
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
    now = datetime(2026, 5, 27, 4, 0, tzinfo=timezone.utc)
    assert digest_date_or_previous_day(None, now=now) == "2026-05-26"


def test_schedule_window_accepts_delivery_slack() -> None:
    now = datetime(2026, 5, 27, 4, 4, tzinfo=timezone.utc)
    assert is_within_schedule_window(now, "09:00", "Asia/Almaty")
    late = datetime(2026, 5, 27, 4, 6, tzinfo=timezone.utc)
    assert not is_within_schedule_window(late, "09:00", "Asia/Almaty")
