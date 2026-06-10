from __future__ import annotations

import httpx

from crypto_monitor.collectors.html import HtmlCollector
from crypto_monitor.models import SourceConfig, SourceType

LISTING_HTML = """
<html><body>
<nav><a href="/login">Войти в личный кабинет пользователя</a></nav>
<main>
  <a href="/news/afsa-fines-veritas">AFSA оштрафовало Veritas Group Limited</a>
  <a href="/news/afsa-guidance-fees">AFSA выпустило руководство по сборам</a>
  <a href="/tag/regulation">regulation tag link long enough here</a>
  <a href="https://twitter.com/afsa">Подпишитесь на наш Twitter канал тут</a>
  <a href="https://notafsa.aifc.kz/news/phishing">Поддельный домен с длинным заголовком тут</a>
  <a href="/news/short">short</a>
</main>
</body></html>
"""


def _source() -> SourceConfig:
    return SourceConfig(
        id="afsa-aifc",
        name="AFSA",
        url="https://afsa.aifc.kz/news",
        type=SourceType.HTML,
        html_list=True,
        language_hint="ru",
    )


def _article_page(title: str, image: str, date: str) -> str:
    return (
        "<html><head>"
        f'<meta property="og:title" content="{title}" />'
        '<meta property="og:description" content="Регулятор МФЦА сообщил детали." />'
        f'<meta property="article:published_time" content="{date}" />'
        f'<meta property="og:image" content="{image}" />'
        "</head><body><article><p>"
        "Текст пресс-релиза регулятора о деятельности в сфере цифровых активов."
        "</p></article></body></html>"
    )


def test_html_list_extracts_individual_articles_with_dates() -> None:
    # Distinct page per article so the test fails if everything collapses to one.
    pages = {
        "/news/afsa-fines-veritas": _article_page(
            "AFSA оштрафовало Veritas Group Limited",
            "https://afsa.aifc.kz/img/veritas.jpg",
            "2026-06-03T10:00:00+05:00",
        ),
        "/news/afsa-guidance-fees": _article_page(
            "AFSA выпустило руководство по сборам",
            "https://afsa.aifc.kz/img/guidance.jpg",
            "2026-06-02T09:00:00+05:00",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(200, text=LISTING_HTML)
        return httpx.Response(200, text=pages[request.url.path])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    articles = HtmlCollector(client=client).collect(_source(), limit=10)

    # Two real headlines pass the filters; nav/tag/social/short are excluded.
    assert len(articles) == 2
    urls = {a.source_url for a in articles}
    assert urls == {
        "https://afsa.aifc.kz/news/afsa-fines-veritas",
        "https://afsa.aifc.kz/news/afsa-guidance-fees",
    }
    # Distinct per-article parsing: titles, images and dates differ per page.
    titles = {a.title for a in articles}
    assert any("Veritas" in t for t in titles)
    assert any("руководство" in t for t in titles)
    images = {a.image_url for a in articles}
    assert images == {
        "https://afsa.aifc.kz/img/veritas.jpg",
        "https://afsa.aifc.kz/img/guidance.jpg",
    }
    dates = {a.published_at.date().isoformat() for a in articles if a.published_at}
    assert dates == {"2026-06-03", "2026-06-02"}


def test_html_list_degrades_to_title_only_on_article_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(200, text=LISTING_HTML)
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    articles = HtmlCollector(client=client).collect(_source(), limit=10)

    # Still emits items (title-only) instead of dropping the source.
    assert len(articles) == 2
    for a in articles:
        assert a.title
        assert a.published_at is not None  # collection-time fallback
        assert a.raw.get("degraded") is True


def test_html_single_article_gets_published_fallback_when_missing() -> None:
    page = "<html><head><title>Пресс-релизы</title></head><body><p>"
    page += "Контент пресс-службы без даты публикации в разметке страницы.</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = SourceConfig(
        id="nationalbank-kz",
        name="NBK",
        url="https://nationalbank.kz/ru/news",
        type=SourceType.HTML,
        html_list=False,
        language_hint="ru",
    )
    articles = HtmlCollector(client=client).collect(source, limit=5)
    assert len(articles) == 1
    # No date in markup -> fallback to collection time, never None.
    assert articles[0].published_at is not None
