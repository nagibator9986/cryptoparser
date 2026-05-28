from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from crypto_monitor.models import Digest, ProcessedArticle, SourceConfig, SourceType
from crypto_monitor.storage import SqliteStorage

Priority = Literal["low", "medium", "high", "critical"]


def test_storage_digest_archive_and_audit(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    digest = Digest(
        digest_date="2026-05-24",
        html="<html></html>",
        plain_text="plain",
        telegram_segments=["segment"],
        stats={"total_articles": 1},
    )

    storage.save_digest(digest)
    storage.log_event("test", {"ok": True})

    assert storage.load_digest("2026-05-24") == digest
    assert storage.list_digests()[0]["digest_date"] == "2026-05-24"
    assert storage.list_audit_events()[0]["event_type"] == "test"
    assert storage.export_json()["audit_events"] == 1


def test_storage_telegram_chat_settings(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    settings = storage.get_or_create_telegram_chat_settings("-1001", "Crypto Desk")
    settings.digest_time = "09:05"
    settings.source_ids = ["coindesk"]
    storage.save_telegram_chat_settings(settings)

    loaded = storage.load_telegram_chat_settings("-1001")
    assert loaded is not None
    assert loaded.digest_time == "09:05"
    assert loaded.source_ids == ["coindesk"]
    assert storage.list_telegram_chat_settings()[0].chat_title == "Crypto Desk"
    assert storage.export_json()["telegram_chats"] == 1


def test_storage_processed_articles_for_digest_filters_date_and_priority(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    storage.save_processed_articles(
        [
            _processed_article(
                "a1",
                published_at=datetime(2026, 5, 26, 9, 0, tzinfo=ZoneInfo("Asia/Almaty")),
                priority="high",
            ),
            _processed_article(
                "a2",
                published_at=datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Almaty")),
                priority="critical",
            ),
            _processed_article(
                "a3",
                published_at=datetime(2026, 5, 26, 10, 0, tzinfo=ZoneInfo("Asia/Almaty")),
                priority="low",
            ),
        ]
    )

    result = storage.load_processed_articles_for_digest(
        "2026-05-26",
        min_priority="medium",
    )

    assert [article.id for article in result] == ["a1"]


def test_storage_source_status_and_search(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    source = SourceConfig(
        id="afsa",
        name="AFSA",
        url="https://afsa.aifc.kz/",
        type=SourceType.HTML,
    )
    storage.record_source_error(source, OSError("timeout"))
    storage.record_source_success(source, article_count=2)
    storage.save_processed_articles(
        [
            _processed_article(
                "a1",
                title="AFSA выдало криптолицензию",
                published_at=datetime(2026, 5, 26, 9, 0, tzinfo=ZoneInfo("Asia/Almaty")),
            )
        ]
    )

    statuses = storage.list_source_statuses()
    search = storage.search_archive("криптолицензию", kind="processed")

    assert statuses[0]["last_article_count"] == 2
    assert statuses[0]["consecutive_failures"] == 0
    assert search["processed_articles"][0]["id"] == "a1"


def _processed_article(
    article_id: str,
    *,
    title: str = "AFSA выдало лицензию криптопровайдеру",
    published_at: datetime,
    priority: str = "high",
) -> ProcessedArticle:
    return ProcessedArticle(
        id=article_id,
        source_id="afsa",
        source_name="AFSA",
        source_url="https://afsa.aifc.kz/news",
        title=title,
        body="AFSA сообщило о лицензировании поставщика услуг цифровых активов.",
        published_at=published_at,
        language="ru",
        title_ru=title,
        summary="AFSA сообщило о лицензировании поставщика услуг цифровых активов.",
        topics=["regulation", "licensing"],
        country="KZ",
        geo_priority=1,
        priority=cast(Priority, priority),
        score=80,
    )
