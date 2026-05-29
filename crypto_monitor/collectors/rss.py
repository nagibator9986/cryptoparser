from __future__ import annotations

import hashlib
import re
from html import unescape
from typing import Any

import feedparser
import httpx

from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import (
    http_response_metadata,
    normalize_image_url,
    normalize_raw_article,
    parse_datetime,
)

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
USER_AGENT = (
    "Mozilla/5.0 (compatible; CryptoMonitor/0.2; "
    "+https://github.com/nagibator9986/cryptoparser)"
)


class RssCollector:
    """RSS/Atom collector backed by feedparser.

    feedparser handles tag variants (RSS 0.9/1.0/2.0, Atom 1.0, namespaces),
    pubDate/published/updated normalization, and known extensions like
    Media RSS and iTunes. We only fetch the bytes via httpx so the timeout,
    redirects, and audit metadata stay consistent with the other collectors.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        response = httpx.get(
            str(source.url),
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"},
        )
        response.raise_for_status()
        response_meta = http_response_metadata(response)
        parsed = feedparser.parse(response.content)

        articles: list[RawArticle] = []
        for entry in parsed.entries[:limit]:
            title = clean_html(_first_attr(entry, ["title"]) or "Untitled")
            link = self._entry_link(entry) or str(source.url)
            body = self._entry_body(entry) or title
            published = self._entry_datetime(entry)
            image_url, image_urls = self._entry_images(entry, body)
            author = self._entry_author(entry)

            article_id = hashlib.sha256(
                f"{source.id}:{link}:{title}".encode()
            ).hexdigest()[:24]

            articles.append(
                normalize_raw_article(
                    RawArticle(
                        id=article_id,
                        source_id=source.id,
                        source_name=source.name,
                        source_url=link,
                        title=title,
                        body=clean_html(body),
                        published_at=published,
                        language=source.language_hint,
                        image_url=image_url,
                        image_urls=image_urls,
                        author=author,
                        raw={
                            "collector": "rss",
                            "response": response_meta,
                            "feed_title": parsed.feed.get("title")
                            if hasattr(parsed, "feed")
                            else None,
                        },
                    )
                )
            )
        return articles

    @staticmethod
    def _entry_link(entry: Any) -> str | None:
        link = entry.get("link")
        if isinstance(link, str) and link:
            return link
        for candidate in entry.get("links", []) or []:
            href = candidate.get("href") if isinstance(candidate, dict) else None
            if href:
                return str(href)
        guid = entry.get("id") or entry.get("guid")
        if isinstance(guid, str) and guid.startswith("http"):
            return guid
        return None

    @staticmethod
    def _entry_body(entry: Any) -> str:
        content = entry.get("content")
        if isinstance(content, list) and content:
            value = content[0].get("value")
            if value:
                return str(value)
        for key in ("summary", "description", "subtitle"):
            value = entry.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _entry_datetime(entry: Any):
        return parse_datetime(
            entry.get("published")
            or entry.get("updated")
            or entry.get("created")
        )

    @staticmethod
    def _entry_author(entry: Any) -> str | None:
        author = entry.get("author")
        if isinstance(author, str) and author:
            return author
        detail = entry.get("author_detail")
        if isinstance(detail, dict):
            name = detail.get("name")
            if name:
                return str(name)
        return None

    @staticmethod
    def _entry_images(entry: Any, body_html: str) -> tuple[str | None, list[str]]:
        seen: list[str] = []

        def add(value: object) -> None:
            normalized = normalize_image_url(value)
            if normalized and normalized not in seen:
                seen.append(normalized)

        for enclosure in entry.get("enclosures", []) or []:
            if isinstance(enclosure, dict):
                etype = str(enclosure.get("type") or "")
                if not etype or etype.startswith("image/"):
                    add(enclosure.get("href") or enclosure.get("url"))

        media_content = entry.get("media_content") or []
        for item in media_content if isinstance(media_content, list) else []:
            if isinstance(item, dict):
                add(item.get("url"))

        media_thumb = entry.get("media_thumbnail") or []
        for item in media_thumb if isinstance(media_thumb, list) else []:
            if isinstance(item, dict):
                add(item.get("url"))

        image = entry.get("image")
        if isinstance(image, dict):
            add(image.get("href") or image.get("url"))
        elif isinstance(image, str):
            add(image)

        itunes_image = entry.get("itunes_image")
        if isinstance(itunes_image, dict):
            add(itunes_image.get("href"))

        for match in _IMG_SRC_RE.finditer(body_html or ""):
            add(match.group(1))

        return (seen[0] if seen else None), seen


def clean_html(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value))
    return re.sub(r"\s+", " ", text).strip()


def _first_attr(entry: Any, names: list[str]) -> str | None:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None
