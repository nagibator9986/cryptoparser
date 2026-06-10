from __future__ import annotations

import json

import httpx
import pytest

from crypto_monitor.collectors.gov_kz import GovKzCollector, _slug_from_url
from crypto_monitor.models import SourceConfig, SourceType

NEWS_RESPONSE = {
    "data": {
        "news": [
            {
                "id": "1237295",
                "title": "Дропперство: меры противодействия и ответственность",
                "slug": "dropperstvo-mery-protivodeystviya",
                "created_date": "2026-06-09T11:48:00Z",
                "heropic": "/uploads/2026/6/9/abc_original.png",
                "alt_image": None,
                "body": "<p>Дропперство &ndash; это схема с банковскими картами.</p>",
                "short_description": None,
            },
            {
                "id": "1237560",
                "title": "О состоянии банковского сектора Казахстана",
                "slug": "o-sostoyanii-bankovskogo-sektora",
                "created_date": "2026-06-09T16:03:00Z",
                "heropic": None,
                "body": None,
                "short_description": "Краткое описание состояния сектора.",
            },
        ]
    }
}


def _source() -> SourceConfig:
    return SourceConfig(
        id="ardfm",
        name="ARDFM",
        url="https://www.gov.kz/memleket/entities/ardfm?lang=ru",
        type=SourceType.GOV_KZ,
        gov_kz_project="ardfm",
        language_hint="ru",
    )


def test_gov_kz_collector_parses_graphql_news() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=NEWS_RESPONSE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    articles = GovKzCollector(client=client).collect(_source(), limit=5)

    # Correct endpoint and EQ-filter encoding.
    assert captured["url"] == "https://www.gov.kz/graphql"
    assert captured["body"]["variables"]["projects"] == "EQ:ardfm"
    assert captured["body"]["variables"]["sort"] == "created_date:DESC"

    assert len(articles) == 2
    first = articles[0]
    assert first.title.startswith("Дропперство")
    # Body HTML is stripped and entities decoded.
    assert "&ndash;" not in first.body
    assert "–" in first.body
    # Real per-article detail URL, not the listing page.
    assert first.source_url == (
        "https://www.gov.kz/memleket/entities/ardfm/press/news/details/"
        "dropperstvo-mery-protivodeystviya?lang=ru"
    )
    assert first.published_at is not None
    assert first.published_at.date().isoformat() == "2026-06-09"
    assert first.image_url == "https://www.gov.kz/uploads/2026/6/9/abc_original.png"

    # Second item: no heropic, body falls back to short_description.
    assert articles[1].image_url is None
    assert "состояния сектора" in articles[1].body


def test_gov_kz_collector_raises_on_graphql_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "bad filter"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        GovKzCollector(client=client).collect(_source(), limit=5)


def test_gov_kz_collector_derives_slug_from_url() -> None:
    assert _slug_from_url("https://www.gov.kz/memleket/entities/ardfm?lang=ru") == "ardfm"
    assert _slug_from_url("https://www.gov.kz/memleket/entities/nationalbank/x") == "nationalbank"
    assert _slug_from_url("https://www.gov.kz/memleket/news?lang=ru") is None


def test_gov_kz_collector_tolerates_dict_shaped_news() -> None:
    # Schema drift: `news` returned as a single object, not a list. Must
    # degrade to zero articles, not crash with a slice TypeError.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"news": {"id": "1", "title": "x", "slug": "y"}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert GovKzCollector(client=client).collect(_source(), limit=5) == []


def test_gov_kz_collector_raises_on_non_dict_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        GovKzCollector(client=client).collect(_source(), limit=5)


def test_gov_kz_collector_skips_items_without_title_or_slug() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"news": [{"id": "1", "title": "", "slug": "x"}, {"id": "2"}]}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert GovKzCollector(client=client).collect(_source(), limit=5) == []
