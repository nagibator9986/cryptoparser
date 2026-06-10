from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from crypto_monitor.collectors.rss import build_http_client
from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import (
    http_response_metadata,
    normalize_image_url,
    normalize_raw_article,
    parse_datetime,
)

logger = logging.getLogger(__name__)

# Listing pages link to many things that are not articles. Skip hrefs whose
# path looks like navigation, taxonomy, or utility pages.
_NON_ARTICLE_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|rubric|search|login|signin|register|"
    r"about|contacts?|feedback|rss|sitemap|privacy|terms|subscribe|page)\b"
    r"|\.(rss|xml|pdf|jpg|png)$",
    re.IGNORECASE,
)
_MIN_HEADLINE_CHARS = 30

_NOISE_TAGS = {
    "script",
    "style",
    "noscript",
    "form",
    "nav",
    "aside",
    "footer",
    "header",
    "iframe",
}


class HtmlCollector:
    """Static HTML collector with structured-data extraction.

    Prefers Open Graph and JSON-LD over raw heuristics — these are the
    contracts publishers expect crawlers to honour. Falls back to readable
    paragraphs only when meta tags are absent.
    """

    def __init__(self, timeout: float = 20.0, client: httpx.Client | None = None) -> None:
        self.timeout = timeout
        self._client = client or build_http_client(timeout)

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        response = self._fetch(str(source.url))
        response_meta = http_response_metadata(response)
        base_url = str(response.url)
        soup = BeautifulSoup(response.text, "lxml")

        if source.html_list:
            return self._collect_listing(source, soup, base_url, limit)

        for tag in soup.find_all(list(_NOISE_TAGS)):
            tag.decompose()
        # Use the final (post-redirect) URL so relative images/links in the
        # page resolve correctly, matching the listing branch.
        article = self._article_from_soup(
            source,
            soup,
            url=base_url,
            response_meta=response_meta,
        )
        return [article][:limit]

    def _collect_listing(
        self,
        source: SourceConfig,
        soup: BeautifulSoup,
        base_url: str,
        limit: int,
    ) -> list[RawArticle]:
        """Extract individual articles from a news-list page.

        Pulls headline links from the listing, then fetches each linked page
        and parses it as a single article. A per-article fetch failure degrades
        to a title-only item (still classifiable) rather than dropping it, and
        never aborts the whole source.
        """

        links = self._extract_listing_links(soup, base_url, str(source.url))
        articles: list[RawArticle] = []
        for title, url in links[:limit]:
            try:
                page = self._fetch(url)
                page_soup = BeautifulSoup(page.text, "lxml")
                for tag in page_soup.find_all(list(_NOISE_TAGS)):
                    tag.decompose()
                articles.append(
                    self._article_from_soup(
                        source,
                        page_soup,
                        url=url,
                        response_meta=http_response_metadata(page),
                        listing_title=title,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "html_listing_article_fetch_failed source_id=%s url=%s error=%s",
                    source.id,
                    url,
                    f"{type(exc).__name__}: {exc}",
                )
                articles.append(self._minimal_article(source, title, url))
        return articles

    def _article_from_soup(
        self,
        source: SourceConfig,
        soup: BeautifulSoup,
        *,
        url: str,
        response_meta: dict[str, object],
        listing_title: str | None = None,
    ) -> RawArticle:
        ld = self._extract_json_ld(soup)
        title = (
            _meta(soup, "og:title")
            or _meta(soup, "twitter:title")
            or _title_from_jsonld(ld)
            or (soup.title.get_text(strip=True) if soup.title else "")
            or listing_title
            or source.name
        )
        description = (
            _meta(soup, "og:description")
            or _meta(soup, "twitter:description")
            or _meta(soup, "description")
            or ""
        )
        body = description or self._extract_body(soup) or listing_title or title

        image_url, image_urls = self._collect_images(soup, ld, url)
        published = self._collect_published(soup, ld)
        estimated = published is None
        author = self._collect_author(soup, ld)

        article_id = hashlib.sha256(f"{source.id}:{url}".encode()).hexdigest()[:24]
        return normalize_raw_article(
            RawArticle(
                id=article_id,
                source_id=source.id,
                source_name=source.name,
                source_url=url,
                title=title.strip(),
                body=body.strip(),
                published_at=published or _utc_now(),
                published_at_estimated=estimated,
                language=source.language_hint,
                image_url=image_url,
                image_urls=image_urls,
                author=author,
                raw={"collector": "html", "response": response_meta},
            )
        )

    def _minimal_article(self, source: SourceConfig, title: str, url: str) -> RawArticle:
        article_id = hashlib.sha256(f"{source.id}:{url}".encode()).hexdigest()[:24]
        return normalize_raw_article(
            RawArticle(
                id=article_id,
                source_id=source.id,
                source_name=source.name,
                source_url=url,
                title=title.strip(),
                body=title.strip(),
                published_at=_utc_now(),
                published_at_estimated=True,
                language=source.language_hint,
                raw={"collector": "html", "degraded": True},
            )
        )

    @staticmethod
    def _extract_listing_links(
        soup: BeautifulSoup,
        base_url: str,
        source_url: str,
    ) -> list[tuple[str, str]]:
        source_host = _registrable_host(urlparse(source_url).netloc)
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
            if len(text) < _MIN_HEADLINE_CHARS:
                continue  # headlines are substantial; menu items are short
            abs_url = urljoin(base_url, str(anchor.get("href")))
            parsed = urlparse(abs_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            link_host = _registrable_host(parsed.netloc)
            if source_host and not (
                link_host == source_host or link_host.endswith("." + source_host)
            ):
                continue  # same host or true subdomain; evilkase.kz must not match kase.kz
            if _NON_ARTICLE_PATH_RE.search(parsed.path):
                continue
            key = abs_url.split("#", 1)[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            items.append((text, abs_url))
        return items

    def _fetch(self, url: str) -> httpx.Response:
        response = self._client.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,*/*"},
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _extract_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(tag.get_text() or "{}")
            except (ValueError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    type_value = candidate.get("@type") or ""
                    if isinstance(type_value, list):
                        type_value = ",".join(str(t) for t in type_value)
                    if "Article" in str(type_value) or "NewsArticle" in str(type_value):
                        return candidate
        return {}

    @staticmethod
    def _extract_body(soup: BeautifulSoup) -> str:
        article = soup.find("article")
        candidate = article if isinstance(article, Tag) else soup
        paragraphs: list[str] = []
        for tag_name in ("p", "li"):
            for element in candidate.find_all(tag_name):
                text = re.sub(r"\s+", " ", element.get_text(" ", strip=True))
                if len(text) >= 30:
                    paragraphs.append(text)
                if len(paragraphs) >= 60:
                    break
            if len(paragraphs) >= 60:
                break
        return "\n".join(paragraphs)

    @staticmethod
    def _collect_images(
        soup: BeautifulSoup,
        ld: dict[str, Any],
        base_url: str,
    ) -> tuple[str | None, list[str]]:
        seen: list[str] = []

        def add(value: object) -> None:
            normalized = normalize_image_url(value, base_url=base_url)
            if normalized and normalized not in seen:
                seen.append(normalized)

        for prop in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
            add(_meta(soup, prop))

        link = soup.find("link", rel="image_src")
        if isinstance(link, Tag):
            add(link.get("href"))

        image_field = ld.get("image")
        if isinstance(image_field, str):
            add(image_field)
        elif isinstance(image_field, dict):
            add(image_field.get("url"))
        elif isinstance(image_field, list):
            for item in image_field:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    add(item.get("url"))

        for img in soup.find_all("img"):
            if isinstance(img, Tag):
                add(img.get("src") or img.get("data-src"))
                if len(seen) >= 8:
                    break

        return (seen[0] if seen else None), seen

    @staticmethod
    def _collect_published(soup: BeautifulSoup, ld: dict[str, Any]):
        for prop in (
            "article:published_time",
            "article:modified_time",
            "og:updated_time",
            "publish-date",
            "pubdate",
        ):
            value = _meta(soup, prop)
            if value:
                parsed = parse_datetime(value)
                if parsed:
                    return parsed
        for key in ("datePublished", "dateCreated", "dateModified"):
            value = ld.get(key)
            if value:
                parsed = parse_datetime(value)
                if parsed:
                    return parsed
        time_tag = soup.find("time")
        if isinstance(time_tag, Tag):
            value = time_tag.get("datetime") or time_tag.get_text(strip=True)
            return parse_datetime(value)
        return None

    @staticmethod
    def _collect_author(soup: BeautifulSoup, ld: dict[str, Any]) -> str | None:
        for prop in ("author", "article:author", "twitter:creator"):
            value = _meta(soup, prop)
            if value:
                return value
        author_field = ld.get("author")
        if isinstance(author_field, dict):
            name = author_field.get("name")
            if name:
                return str(name)
        if isinstance(author_field, list) and author_field:
            first = author_field[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"])
            if isinstance(first, str):
                return first
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _registrable_host(netloc: str) -> str:
    host = netloc.split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    for attr in ("property", "name", "itemprop"):
        tag = soup.find("meta", attrs={attr: name})
        if isinstance(tag, Tag):
            content = tag.get("content")
            if content:
                return str(content).strip()
    return None


def _title_from_jsonld(ld: dict[str, Any]) -> str | None:
    for key in ("headline", "name", "alternativeHeadline"):
        value = ld.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
