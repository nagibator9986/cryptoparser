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


def _article(
    article_id: str,
    *,
    title: str,
    country: str,
    geo_priority: int,
    priority: str,
    score: int,
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
        topics=["regulation", "licensing"],
        country=country,
        geo_priority=geo_priority,
        priority=cast(Priority, priority),
        score=score,
    )
