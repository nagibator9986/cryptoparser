from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from crypto_monitor.collectors.gov_kz import GovKzCollector
from crypto_monitor.collectors.html import HtmlCollector
from crypto_monitor.collectors.json_api import JsonApiCollector
from crypto_monitor.collectors.rss import RssCollector, build_http_client
from crypto_monitor.models import RawArticle, SourceConfig, SourceType

logger = logging.getLogger(__name__)


class SourceStatusRecorder(Protocol):
    def record_source_success(self, source: SourceConfig, article_count: int) -> None:
        ...

    def record_source_error(self, source: SourceConfig, error: Exception) -> None:
        ...


class CollectorRunner:
    def __init__(self) -> None:
        self._client = build_http_client()
        self.rss = RssCollector(client=self._client)
        self.html = HtmlCollector(client=self._client)
        self.json_api = JsonApiCollector(client=self._client)
        self.gov_kz = GovKzCollector(client=self._client)

    def collect_all(
        self,
        sources: list[SourceConfig],
        limit_per_source: int = 20,
        status_recorder: SourceStatusRecorder | None = None,
        concurrency: int = 8,
    ) -> list[RawArticle]:
        if not sources:
            return []
        # Sources are independent network fetches, so they run in parallel.
        # Results are gathered in source order and status is recorded on the
        # calling thread to keep SQLite writes single-threaded.
        max_workers = max(1, min(concurrency, len(sources)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self.collect, source, limit_per_source) for source in sources
            ]
            results = list(zip(sources, futures, strict=True))

        articles: list[RawArticle] = []
        for source, future in results:
            try:
                source_articles = future.result()
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
        if source.type == SourceType.GOV_KZ:
            return self.gov_kz.collect(source, limit=limit)
        raise NotImplementedError(
            f"Collector for {source.type!s} is not implemented in the local MVP. "
            "Telegram, X, and JSON API connectors should be added as source-specific plugins."
        )
