from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from html import unescape

import httpx

from crypto_monitor.models import RawArticle, SourceConfig
from crypto_monitor.normalization import (
    http_response_metadata,
    normalize_raw_article,
    parse_datetime,
)

_TAG_RE = re.compile(r"<[^>]+>")


class RssCollector:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def collect(self, source: SourceConfig, limit: int = 20) -> list[RawArticle]:
        response = httpx.get(str(source.url), timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        response_meta = http_response_metadata(response)
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        if not items:
            items = root.findall("{http://www.w3.org/2005/Atom}entry")

        articles: list[RawArticle] = []
        for item in items[:limit]:
            title = self._first_text(item, ["title"]) or "Untitled"
            link = self._first_text(item, ["link", "guid"]) or str(source.url)
            if item.tag.endswith("entry"):
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None and link_el.attrib.get("href"):
                    link = link_el.attrib["href"]
            body = self._first_text(
                item,
                ["description", "summary", "content", "encoded"],
            ) or title
            published = self._parse_date(
                self._first_text(item, ["pubDate", "published", "updated"])
            )
            article_id = hashlib.sha256(f"{source.id}:{link}:{title}".encode()).hexdigest()[:24]
            articles.append(
                normalize_raw_article(
                    RawArticle(
                        id=article_id,
                        source_id=source.id,
                        source_name=source.name,
                        source_url=link,
                        title=clean_html(title),
                        body=clean_html(body),
                        published_at=published,
                        language=source.language_hint,
                        raw={"collector": "rss", "response": response_meta},
                    )
                )
            )
        return articles

    @staticmethod
    def _first_text(item: ET.Element, names: list[str]) -> str | None:
        expected = set(names)
        for element in item.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            if local_name in expected and element.text:
                return element.text.strip()
        for name in names:
            found = item.find(name)
            if found is None:
                found = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
            if found is not None and found.text:
                return found.text.strip()
        return None

    @staticmethod
    def _parse_date(value: str | None):
        return parse_datetime(value)


def clean_html(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value))
    return re.sub(r"\s+", " ", text).strip()
