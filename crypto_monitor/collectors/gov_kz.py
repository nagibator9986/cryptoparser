from __future__ import annotations

import hashlib
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from crypto_monitor.collectors.rss import build_http_client
from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import (
    normalize_image_url,
    normalize_raw_article,
    parse_datetime,
)

# gov.kz is a React SPA with no static HTML and no documented API. Its public
# Apollo GraphQL backend exposes the `news` query, filtered per government
# entity. The filter DSL (team.alabs.hcms) encodes the operator inside the
# value as "OP:value" — "EQ:<slug>" selects one entity. This is an
# undocumented endpoint: keep requests modest and tolerate schema drift.
GOV_KZ_GRAPHQL_URL = "https://www.gov.kz/graphql"
GOV_KZ_BASE_URL = "https://www.gov.kz"
_NEWS_QUERY = (
    "query News($projects: String, $size: Int, $sort: String, $lang: String) {"
    " news(projects: $projects, _size: $size, _page: 1, _sort: $sort, _lang: $lang)"
    " { id title slug created_date heropic alt_image body short_description } }"
)

_TAG_RE = re.compile(r"<[^>]+>")


class GovKzCollector:
    """Collects an entity's news from the gov.kz public GraphQL API."""

    def __init__(
        self,
        endpoint: str = GOV_KZ_GRAPHQL_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._client = client or build_http_client(timeout)

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        slug = source.gov_kz_project or _slug_from_url(str(source.url))
        if not slug:
            raise ValueError(
                f"gov_kz source {source.id} has no gov_kz_project and no /entities/<slug> URL"
            )
        lang = source.language_hint or "ru"
        response = self._client.post(
            self.endpoint,
            json={
                "query": _NEWS_QUERY,
                "variables": {
                    "projects": f"EQ:{slug}",
                    "size": max(1, limit),
                    "sort": "created_date:DESC",
                    "lang": lang,
                },
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"gov.kz unexpected response shape: {type(payload).__name__}")
        if payload.get("errors"):
            raise RuntimeError(f"gov.kz GraphQL error: {payload['errors']}")
        news = (payload.get("data") or {}).get("news")
        items = news[:limit] if isinstance(news, list) else []

        articles: list[RawArticle] = []
        for item in items:
            article = _build_article(source, slug, item, lang)
            if article is not None:
                articles.append(article)
        return articles


def _build_article(
    source: SourceConfig,
    slug: str,
    item: dict[str, Any],
    lang: str,
) -> RawArticle | None:
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or "").strip()
    news_slug = (item.get("slug") or "").strip()
    if not title or not news_slug:
        return None
    detail_url = (
        f"{GOV_KZ_BASE_URL}/memleket/entities/{slug}/press/news/details/{news_slug}?lang={lang}"
    )
    body = _html_to_text(item.get("body") or "") or (item.get("short_description") or "") or title
    image_url = normalize_image_url(item.get("heropic"), base_url=GOV_KZ_BASE_URL)
    article_id = hashlib.sha256(f"{source.id}:{news_slug}".encode()).hexdigest()[:24]
    return normalize_raw_article(
        RawArticle(
            id=article_id,
            source_id=source.id,
            source_name=source.name,
            source_url=detail_url,
            title=title,
            body=body,
            published_at=parse_datetime(item.get("created_date")),
            language=source.language_hint,
            image_url=image_url,
            image_urls=[image_url] if image_url else [],
            raw={"collector": "gov_kz", "id": item.get("id"), "slug": news_slug},
        )
    )


def _slug_from_url(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "entities" in parts:
        index = parts.index("entities")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _html_to_text(html: str) -> str:
    text = unescape(_TAG_RE.sub(" ", html))
    return re.sub(r"\s+", " ", text).strip()
