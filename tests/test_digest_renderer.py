from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from crypto_monitor.digest_renderer import render_digest_locally
from crypto_monitor.models import ProcessedArticle

Priority = Literal["low", "medium", "high", "critical"]


def test_local_digest_orders_kazakhstan_before_international_and_includes_date() -> None:
    international = _article(
        "int",
        title="SEC обновила подход к криптобиржам",
        country="US",
        geo_priority=3,
        priority="critical",
        score=99,
    )
    kazakhstan = _article(
        "kz",
        title="AFSA выдало лицензию криптопровайдеру",
        country="KZ",
        geo_priority=1,
        priority="high",
        score=80,
    )

    digest = render_digest_locally([international, kazakhstan], digest_date="2026-05-26")

    assert digest.plain_text.find("AFSA") < digest.plain_text.find("SEC")
    assert "Дата: 26.05.2026 09:00" in digest.plain_text
    assert digest.telegram_segments


def test_local_digest_builds_structured_telegram_articles_with_image() -> None:
    article = _article(
        "kz",
        title="AFSA выдало лицензию криптопровайдеру",
        country="KZ",
        geo_priority=1,
        priority="high",
        score=80,
        image_url="https://afsa.aifc.kz/image.jpg",
    )

    digest = render_digest_locally([article], digest_date="2026-05-26")

    assert digest.telegram_articles, "structured articles must be populated"
    block = digest.telegram_articles[0]
    assert block.title == "AFSA выдало лицензию криптопровайдеру"
    assert block.image_url == "https://afsa.aifc.kz/image.jpg"
    assert block.section.startswith("Регулирование")
    assert digest.header_text and "Цифровые активы" in digest.header_text
    assert digest.stats.get("with_image") == 1


def test_legislation_section_shows_first_and_includes_stage_prefix() -> None:
    legislation = _article(
        "leg",
        title="В Мажилис внесён законопроект о цифровых активах",
        country="KZ",
        geo_priority=1,
        priority="critical",
        score=92,
    )
    legislation.is_legislative = True
    legislation.legislative_stage = "introduced"
    other = _article(
        "cbdc",
        title="НБРК запустил вторую фазу цифрового тенге",
        country="KZ",
        geo_priority=1,
        priority="high",
        score=80,
        topics=["cbdc"],
    )

    digest = render_digest_locally([other, legislation], digest_date="2026-05-26")

    assert "Законодательные изменения" in digest.plain_text
    leg_pos = digest.plain_text.find("Законодательные изменения")
    cbdc_pos = digest.plain_text.find("CBDC")
    assert leg_pos < cbdc_pos, "legislation section must appear before CBDC"
    assert "Внесён законопроект:" in digest.plain_text


def test_events_section_renders_event_metadata() -> None:
    event = _article(
        "ev",
        title="AIFC проведёт Astana Finance Days",
        country="KZ",
        geo_priority=1,
        priority="high",
        score=75,
        topics=["events", "regulation"],
    )
    event.event_date = "2025-11-12/2025-11-14"
    event.event_location = "Астана, AIFC"
    event.event_scale = "kz_major"

    digest = render_digest_locally([event], digest_date="2026-05-26")

    assert "Мероприятия и форумы" in digest.plain_text
    assert "Астана, AIFC" in digest.plain_text
    assert "2025-11-12/2025-11-14" in digest.plain_text


def test_low_priority_articles_are_excluded_and_quiet_day_notice_shown() -> None:
    """Replicates the Nobitex live case: only low article in pool → quiet day."""

    iran = _article(
        "iran",
        title="США ввели санкции против Nobitex",
        country="US",
        geo_priority=3,
        priority="low",
        score=20,
        topics=["regulation", "exchanges"],
    )

    digest = render_digest_locally([iran], digest_date="2026-06-03")

    assert "Nobitex" not in digest.plain_text
    assert "не зафиксировано" in digest.plain_text
    assert digest.stats.get("total_articles") == 0
    assert digest.stats.get("quiet_day") is True
    assert digest.telegram_articles == []


def test_low_priority_articles_are_filtered_when_mixed_with_high() -> None:
    iran = _article(
        "iran",
        title="США ввели санкции против Nobitex",
        country="US",
        geo_priority=3,
        priority="low",
        score=20,
        topics=["regulation", "exchanges"],
    )
    kz = _article(
        "kz",
        title="AFSA выдало лицензию криптопровайдеру",
        country="KZ",
        geo_priority=1,
        priority="high",
        score=80,
    )

    digest = render_digest_locally([iran, kz], digest_date="2026-06-03")

    assert "Nobitex" not in digest.plain_text
    assert "AFSA" in digest.plain_text
    assert digest.stats.get("total_articles") == 1


def _article(
    article_id: str,
    *,
    title: str,
    country: str,
    geo_priority: int,
    priority: str,
    score: int,
    image_url: str | None = None,
    topics: list[str] | None = None,
) -> ProcessedArticle:
    return ProcessedArticle(
        id=article_id,
        source_id=article_id,
        source_name=article_id.upper(),
        source_url=f"https://example.com/{article_id}",
        title=title,
        body=f"{title}. Подробности события опубликованы источником.",
        published_at=datetime(2026, 5, 26, 9, 0, tzinfo=ZoneInfo("Asia/Almaty")),
        language="ru",
        title_ru=title,
        summary=f"{title}. Краткое описание события для проверки сортировки.",
        topics=topics if topics is not None else ["regulation", "licensing"],
        country=country,
        geo_priority=geo_priority,
        priority=cast(Priority, priority),
        score=score,
        image_url=image_url,
    )
