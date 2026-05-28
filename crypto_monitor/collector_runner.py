from __future__ import annotations

import logging
from typing import Protocol

from crypto_monitor.collectors.html import HtmlCollector
from crypto_monitor.collectors.json_api import JsonApiCollector
from crypto_monitor.collectors.rss import RssCollector
from crypto_monitor.models import RawArticle, SourceConfig, SourceType

logger = logging.getLogger(__name__)


class SourceStatusRecorder(Protocol):
    def record_source_success(self, source: SourceConfig, article_count: int) -> None:
        ...

    def record_source_error(self, source: SourceConfig, error: Exception) -> None:
        ...


class CollectorRunner:
    def __init__(self) -> None:
        self.rss = RssCollector()
        self.html = HtmlCollector()
        self.json_api = JsonApiCollector()

    def collect_all(
        self,
        sources: list[SourceConfig],
        limit_per_source: int = 20,
        status_recorder: SourceStatusRecorder | None = None,
    ) -> list[RawArticle]:
        articles: list[RawArticle] = []
        for source in sources:
            try:
                source_articles = self.collect(source, limit_per_source)
                articles.extend(source_articles)
                if status_recorder:
                    status_recorder.record_source_success(source, len(source_articles))
            except Exception as exc:
                if status_recorder:
                    status_recorder.record_source_error(source, exc)
                logger.exception(
                    "source_collection_failed source_id=%s url=%s",
                    source.id,
                    source.url,
                )
        return articles

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        if source.type == SourceType.RSS:
            return self.rss.collect(source, limit=limit)
        if source.type == SourceType.HTML:
            return self.html.collect(source, limit=limit)
        if source.type == SourceType.JSON_API:
            return self.json_api.collect(source, limit=limit)
        raise NotImplementedError(
            f"Collector for {source.type!s} is not implemented in the local MVP. "
            "Telegram, X, and JSON API connectors should be added as source-specific plugins."
        )
