from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_monitor.models import (
    CryptoRatesSnapshot,
    Digest,
    ProcessedArticle,
    RawArticle,
    SourceConfig,
    TelegramChatSettings,
)
from crypto_monitor.normalization import is_datetime_in_window


class SqliteStorage:
    """Small durable storage for MVP/local deployment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        # WAL lets the polling bot read while the pipeline writes without
        # blocking each other; busy_timeout makes the rare writer-writer
        # contention wait instead of raising "database is locked".
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=NORMAL")
        connection.execute("pragma busy_timeout=30000")
        connection.execute("pragma foreign_keys=ON")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                create table if not exists raw_articles (
                    id text primary key,
                    source_id text not null,
                    source_name text not null,
                    source_url text not null,
                    title text not null,
                    body text not null,
                    published_at text,
                    language text,
                    payload text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists processed_articles (
                    id text primary key,
                    payload text not null,
                    updated_at text not null default current_timestamp
                );

                create table if not exists digests (
                    digest_date text primary key,
                    payload text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists crypto_rates (
                    rate_date text primary key,
                    payload text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists delivered_articles (
                    chat_id text not null,
                    article_id text not null,
                    sent_at text not null default current_timestamp,
                    primary key (chat_id, article_id)
                );
                create index if not exists idx_delivered_articles_chat
                    on delivered_articles (chat_id, sent_at desc);

                create table if not exists audit_events (
                    id integer primary key autoincrement,
                    event_type text not null,
                    payload text not null,
                    created_at text not null default current_timestamp
                );

                create table if not exists telegram_chat_settings (
                    chat_id text primary key,
                    chat_title text,
                    payload text not null,
                    created_at text not null default current_timestamp,
                    updated_at text not null default current_timestamp
                );

                create table if not exists source_status (
                    source_id text primary key,
                    source_name text not null,
                    source_url text not null,
                    source_type text not null,
                    enabled integer not null,
                    last_success_at text,
                    last_error_at text,
                    last_error text,
                    unavailable_since text,
                    consecutive_failures integer not null default 0,
                    last_article_count integer not null default 0,
                    updated_at text not null default current_timestamp
                );

                create index if not exists idx_raw_articles_source
                    on raw_articles (source_id);
                create index if not exists idx_raw_articles_published
                    on raw_articles (published_at desc);
                create index if not exists idx_processed_articles_updated
                    on processed_articles (updated_at desc);
                create index if not exists idx_digests_created
                    on digests (created_at desc);
                create index if not exists idx_audit_events_created
                    on audit_events (id desc);
                """
            )

    def save_raw_articles(self, articles: Iterable[RawArticle]) -> int:
        count = 0
        with self._connect() as db:
            for article in articles:
                payload = article.model_dump_json()
                cursor = db.execute(
                    """
                    insert or ignore into raw_articles
                    (
                        id, source_id, source_name, source_url, title,
                        body, published_at, language, payload
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article.id,
                        article.source_id,
                        article.source_name,
                        article.source_url,
                        article.title,
                        article.body,
                        article.published_at.isoformat() if article.published_at else None,
                        article.language,
                        payload,
                    ),
                )
                count += max(cursor.rowcount, 0)
        return count

    def load_raw_articles(self, limit: int = 100) -> list[RawArticle]:
        with self._connect() as db:
            rows = db.execute(
                """
                select payload from raw_articles
                order by coalesce(published_at, created_at) desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [RawArticle.model_validate_json(row["payload"]) for row in rows]

    def save_processed_articles(self, articles: Iterable[ProcessedArticle]) -> None:
        with self._connect() as db:
            for article in articles:
                db.execute(
                    """
                    insert into processed_articles (id, payload, updated_at)
                    values (?, ?, current_timestamp)
                    on conflict(id) do update set
                        payload=excluded.payload,
                        updated_at=current_timestamp
                    """,
                    (article.id, article.model_dump_json()),
                )

    def load_processed_articles(self, limit: int | None = 100) -> list[ProcessedArticle]:
        with self._connect() as db:
            if limit is None:
                rows = db.execute(
                    "select payload from processed_articles order by updated_at desc"
                ).fetchall()
            else:
                rows = db.execute(
                    "select payload from processed_articles order by updated_at desc limit ?",
                    (limit,),
                ).fetchall()
        return [ProcessedArticle.model_validate_json(row["payload"]) for row in rows]

    def load_processed_articles_for_digest(
        self,
        digest_date: str,
        *,
        limit: int = 25,
        timezone_name: str = "Asia/Almaty",
        source_ids: list[str] | None = None,
        min_priority: str | None = None,
        lookback_days: int = 1,
    ) -> list[ProcessedArticle]:
        articles = self.load_processed_articles(limit=None)
        selected_sources = set(source_ids or [])
        priority_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        min_rank = priority_rank.get(min_priority or "low", 1)
        filtered = []
        for article in articles:
            if selected_sources and article.source_id not in selected_sources:
                continue
            if not is_datetime_in_window(
                article.published_at, digest_date, lookback_days, timezone_name
            ):
                continue
            if priority_rank.get(article.priority or "medium", 2) < min_rank:
                continue
            filtered.append(article)
        return sorted(filtered, key=_article_sort_key)[:limit]

    def save_digest(self, digest: Digest) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into digests (digest_date, payload, created_at)
                values (?, ?, current_timestamp)
                on conflict(digest_date) do update set
                    payload=excluded.payload,
                    created_at=current_timestamp
                """,
                (digest.digest_date, digest.model_dump_json()),
            )

    def load_digest(self, digest_date: str) -> Digest | None:
        with self._connect() as db:
            row = db.execute(
                "select payload from digests where digest_date = ?",
                (digest_date,),
            ).fetchone()
        return Digest.model_validate_json(row["payload"]) if row else None

    def list_digests(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                select digest_date, created_at, payload
                from digests
                order by created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            digest = Digest.model_validate_json(row["payload"])
            result.append(
                {
                    "digest_date": row["digest_date"],
                    "created_at": row["created_at"],
                    "telegram_segments": len(digest.telegram_segments),
                    "total_articles": digest.stats.get("total_articles"),
                }
            )
        return result

    def save_rates_snapshot(self, snapshot: CryptoRatesSnapshot) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into crypto_rates (rate_date, payload, created_at)
                values (?, ?, current_timestamp)
                on conflict(rate_date) do update set
                    payload=excluded.payload,
                    created_at=current_timestamp
                """,
                (snapshot.date, snapshot.model_dump_json()),
            )

    def load_latest_rates_snapshot(self) -> CryptoRatesSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "select payload from crypto_rates order by rate_date desc limit 1"
            ).fetchone()
        return CryptoRatesSnapshot.model_validate_json(row["payload"]) if row else None

    def record_delivered_articles(self, chat_id: str, article_ids: Iterable[str]) -> None:
        ids = [aid for aid in article_ids if aid]
        if not ids:
            return
        with self._connect() as db:
            db.executemany(
                """
                insert into delivered_articles (chat_id, article_id, sent_at)
                values (?, ?, current_timestamp)
                on conflict(chat_id, article_id) do update set sent_at=current_timestamp
                """,
                [(chat_id, aid) for aid in ids],
            )

    def load_delivered_article_ids(self, chat_id: str, within_days: int = 30) -> set[str]:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, within_days))).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """
                select article_id from delivered_articles
                where chat_id = ? and sent_at >= ?
                """,
                (chat_id, cutoff),
            ).fetchall()
        return {row["article_id"] for row in rows}

    def log_event(self, event_type: str, payload: dict) -> None:
        with self._connect() as db:
            db.execute(
                "insert into audit_events (event_type, payload) values (?, ?)",
                (event_type, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def list_audit_events(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                select id, event_type, payload, created_at
                from audit_events
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_or_create_telegram_chat_settings(
        self,
        chat_id: str,
        chat_title: str | None = None,
    ) -> TelegramChatSettings:
        existing = self.load_telegram_chat_settings(chat_id)
        if existing:
            if chat_title and chat_title != existing.chat_title:
                existing.chat_title = chat_title
                self.save_telegram_chat_settings(existing)
            return existing

        settings = TelegramChatSettings(chat_id=chat_id, chat_title=chat_title)
        self.save_telegram_chat_settings(settings)
        return settings

    def save_telegram_chat_settings(self, settings: TelegramChatSettings) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into telegram_chat_settings (chat_id, chat_title, payload, updated_at)
                values (?, ?, ?, current_timestamp)
                on conflict(chat_id) do update set
                    chat_title=excluded.chat_title,
                    payload=excluded.payload,
                    updated_at=current_timestamp
                """,
                (
                    settings.chat_id,
                    settings.chat_title,
                    settings.model_dump_json(),
                ),
            )

    def load_telegram_chat_settings(self, chat_id: str) -> TelegramChatSettings | None:
        with self._connect() as db:
            row = db.execute(
                "select payload from telegram_chat_settings where chat_id = ?",
                (chat_id,),
            ).fetchone()
        return TelegramChatSettings.model_validate_json(row["payload"]) if row else None

    def list_telegram_chat_settings(
        self,
        only_enabled: bool = False,
    ) -> list[TelegramChatSettings]:
        with self._connect() as db:
            rows = db.execute(
                """
                select payload
                from telegram_chat_settings
                order by updated_at desc
                """
            ).fetchall()
        chats = [TelegramChatSettings.model_validate_json(row["payload"]) for row in rows]
        if only_enabled:
            return [chat for chat in chats if chat.enabled]
        return chats

    def record_source_success(self, source: SourceConfig, article_count: int) -> None:
        now = _utc_now()
        with self._connect() as db:
            db.execute(
                """
                insert into source_status
                (
                    source_id, source_name, source_url, source_type, enabled,
                    last_success_at, last_error_at, last_error, unavailable_since,
                    consecutive_failures, last_article_count, updated_at
                )
                values (?, ?, ?, ?, ?, ?, null, null, null, 0, ?, ?)
                on conflict(source_id) do update set
                    source_name=excluded.source_name,
                    source_url=excluded.source_url,
                    source_type=excluded.source_type,
                    enabled=excluded.enabled,
                    last_success_at=excluded.last_success_at,
                    last_error_at=null,
                    last_error=null,
                    unavailable_since=null,
                    consecutive_failures=0,
                    last_article_count=excluded.last_article_count,
                    updated_at=excluded.updated_at
                """,
                (
                    source.id,
                    source.name,
                    str(source.url),
                    str(source.type),
                    int(source.enabled),
                    now,
                    article_count,
                    now,
                ),
            )

    def record_source_error(self, source: SourceConfig, error: Exception) -> None:
        now = _utc_now()
        with self._connect() as db:
            existing = db.execute(
                """
                select unavailable_since, consecutive_failures
                from source_status
                where source_id = ?
                """,
                (source.id,),
            ).fetchone()
            unavailable_since = existing["unavailable_since"] if existing else now
            failures = int(existing["consecutive_failures"] if existing else 0) + 1
            db.execute(
                """
                insert into source_status
                (
                    source_id, source_name, source_url, source_type, enabled,
                    last_success_at, last_error_at, last_error, unavailable_since,
                    consecutive_failures, last_article_count, updated_at
                )
                values (?, ?, ?, ?, ?, null, ?, ?, ?, ?, 0, ?)
                on conflict(source_id) do update set
                    source_name=excluded.source_name,
                    source_url=excluded.source_url,
                    source_type=excluded.source_type,
                    enabled=excluded.enabled,
                    last_error_at=excluded.last_error_at,
                    last_error=excluded.last_error,
                    unavailable_since=coalesce(
                        source_status.unavailable_since,
                        excluded.unavailable_since
                    ),
                    consecutive_failures=excluded.consecutive_failures,
                    updated_at=excluded.updated_at
                """,
                (
                    source.id,
                    source.name,
                    str(source.url),
                    str(source.type),
                    int(source.enabled),
                    now,
                    f"{type(error).__name__}: {error}",
                    unavailable_since,
                    failures,
                    now,
                ),
            )

    def list_source_statuses(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                select *
                from source_status
                order by updated_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "source_url": row["source_url"],
                "source_type": row["source_type"],
                "enabled": bool(row["enabled"]),
                "last_success_at": row["last_success_at"],
                "last_error_at": row["last_error_at"],
                "last_error": row["last_error"],
                "unavailable_since": row["unavailable_since"],
                "needs_alert": _needs_source_alert(row["unavailable_since"]),
                "consecutive_failures": row["consecutive_failures"],
                "last_article_count": row["last_article_count"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def search_archive(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str = "all",
        source_id: str | None = None,
        topic: str | None = None,
        country: str | None = None,
    ) -> dict[str, list[dict]]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            raise ValueError("query must not be empty")
        result: dict[str, list[dict]] = {
            "raw_articles": [],
            "processed_articles": [],
            "digests": [],
        }
        if kind in {"all", "raw", "articles"}:
            result["raw_articles"] = self._search_raw_articles(
                normalized_query,
                limit=limit,
                source_id=source_id,
            )
        if kind in {"all", "processed", "articles"}:
            result["processed_articles"] = self._search_processed_articles(
                normalized_query,
                limit=limit,
                source_id=source_id,
                topic=topic,
                country=country,
            )
        if kind in {"all", "digests"}:
            result["digests"] = self._search_digests(normalized_query, limit=limit)
        return result

    def export_json(self) -> dict:
        with self._connect() as db:
            raw_count = db.execute("select count(*) as n from raw_articles").fetchone()["n"]
            processed_count = db.execute(
                "select count(*) as n from processed_articles"
            ).fetchone()["n"]
            digest_count = db.execute("select count(*) as n from digests").fetchone()["n"]
            audit_count = db.execute("select count(*) as n from audit_events").fetchone()["n"]
            telegram_chat_count = db.execute(
                "select count(*) as n from telegram_chat_settings"
            ).fetchone()["n"]
            source_status_count = db.execute(
                "select count(*) as n from source_status"
            ).fetchone()["n"]
        return {
            "path": str(self.path),
            "raw_articles": raw_count,
            "processed_articles": processed_count,
            "digests": digest_count,
            "audit_events": audit_count,
            "telegram_chats": telegram_chat_count,
            "source_statuses": source_status_count,
        }

    def _search_raw_articles(
        self,
        query: str,
        *,
        limit: int,
        source_id: str | None,
    ) -> list[dict]:
        like = f"%{query}%"
        sql = """
            select id, source_id, source_name, source_url, title, published_at, language
            from raw_articles
            where (lower(title) like ? or lower(body) like ? or lower(source_name) like ?)
        """
        params: list[object] = [like, like, like]
        if source_id:
            sql += " and source_id = ?"
            params.append(source_id)
        sql += " order by coalesce(published_at, created_at) desc limit ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _search_processed_articles(
        self,
        query: str,
        *,
        limit: int,
        source_id: str | None,
        topic: str | None,
        country: str | None,
    ) -> list[dict]:
        matches = []
        for article in self.load_processed_articles(limit=None):
            haystack = " ".join(
                [
                    article.title,
                    article.title_ru or "",
                    article.summary or "",
                    article.body,
                    article.source_name,
                    " ".join(article.topics),
                    article.country or "",
                ]
            ).lower()
            if query not in haystack:
                continue
            if source_id and article.source_id != source_id:
                continue
            if topic and topic not in article.topics:
                continue
            if country and (article.country or "").lower() != country.lower():
                continue
            matches.append(
                {
                    "id": article.id,
                    "source_id": article.source_id,
                    "source_name": article.source_name,
                    "source_url": article.source_url,
                    "title": article.title_ru or article.title,
                    "published_at": (
                        article.published_at.isoformat() if article.published_at else None
                    ),
                    "topics": article.topics,
                    "country": article.country,
                    "geo_priority": article.geo_priority,
                    "priority": article.priority,
                    "score": article.score,
                }
            )
            if len(matches) >= limit:
                break
        return matches

    def _search_digests(self, query: str, *, limit: int) -> list[dict]:
        like = f"%{query}%"
        with self._connect() as db:
            rows = db.execute(
                """
                select digest_date, created_at, payload
                from digests
                where lower(payload) like ?
                order by created_at desc
                limit ?
                """,
                (like, limit),
            ).fetchall()
        return [
            {
                "digest_date": row["digest_date"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def dumps_pretty(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _needs_source_alert(unavailable_since: str | None) -> bool:
    if not unavailable_since:
        return False
    try:
        started = datetime.fromisoformat(unavailable_since)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return datetime.now(UTC) - started >= timedelta(hours=2)


def _article_sort_key(article: ProcessedArticle) -> tuple[int, int, int, float]:
    priority_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    geo_priority = article.geo_priority if article.geo_priority in {1, 2, 3} else 4
    published = article.published_at.timestamp() if article.published_at else 0.0
    return (
        geo_priority,
        -priority_rank.get(article.priority or "medium", 2),
        -(article.score or 0),
        -published,
    )
