from __future__ import annotations

import hashlib
from typing import Any

import httpx

from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import (
    http_response_metadata,
    normalize_image_url,
    normalize_raw_article,
    parse_datetime,
)


class JsonApiCollector:
    """Generic JSON API collector.

    Supports common response shapes:
    - a top-level list of objects;
    - a dict with one of: items, articles, news, results, data.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        response = httpx.get(str(source.url), timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        response_meta = http_response_metadata(response)
        payload = response.json()
        items = _extract_items(payload)

        articles: list[RawArticle] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            title = _first(item, ["title", "headline", "name"]) or source.name
            body = _first(item, ["body", "content", "summary", "description", "text"]) or title
            url = _first(item, ["url", "link", "source_url"]) or str(source.url)
            published_raw = _first(item, ["published_at", "published", "pubDate", "date"])
            published_at = parse_datetime(published_raw)
            article_id = str(
                _first(item, ["id", "guid"])
                or hashlib.sha256(f"{source.id}:{url}:{title}".encode()).hexdigest()[:24]
            )
            image_candidates = [
                _first(item, ["image", "image_url", "thumbnail", "cover_image", "photo"]),
            ]
            image_url = None
            image_urls: list[str] = []
            for candidate in image_candidates:
                normalized = normalize_image_url(candidate)
                if normalized and normalized not in image_urls:
                    image_urls.append(normalized)
                    image_url = image_url or normalized
            author = _first(item, ["author", "creator", "byline"])
            articles.append(
                normalize_raw_article(
                    RawArticle(
                        id=article_id,
                        source_id=source.id,
                        source_name=source.name,
                        source_url=str(url),
                        title=str(title),
                        body=str(body),
                        published_at=published_at,
                        language=source.language_hint,
                        image_url=image_url,
                        image_urls=image_urls,
                        author=str(author) if author else None,
                        raw={
                            "collector": "json_api",
                            "item": item,
                            "response": response_meta,
                        },
                    )
                )
            )
        return articles


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "articles", "news", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _first(item: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None

