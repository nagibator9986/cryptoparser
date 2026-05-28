from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

import httpx

from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import http_response_metadata, normalize_raw_article


class _ReadableTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.paragraphs: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        current = self._tag_stack[-1] if self._tag_stack else ""
        if current == "title":
            self.title.append(text)
        elif any(tag in {"p", "h1", "h2", "li"} for tag in self._tag_stack) and len(text) > 20:
            self.paragraphs.append(text)


class HtmlCollector:
    """Generic HTML collector for static pages.

    For production, source-specific parsers should subclass or replace this
    collector. This implementation is intentionally conservative and dependency-light.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        response = httpx.get(str(source.url), timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        response_meta = http_response_metadata(response)
        parser = _ReadableTextParser()
        parser.feed(response.text)
        title = " ".join(parser.title[:1]).strip() or source.name
        body = "\n".join(parser.paragraphs[:80]).strip() or title
        article_id = hashlib.sha256(f"{source.id}:{source.url}:{title}".encode()).hexdigest()[:24]
        return [
            normalize_raw_article(
                RawArticle(
                    id=article_id,
                    source_id=source.id,
                    source_name=source.name,
                    source_url=str(source.url),
                    title=title,
                    body=body,
                    language=source.language_hint,
                    raw={"collector": "html", "response": response_meta},
                )
            )
        ][:limit]
