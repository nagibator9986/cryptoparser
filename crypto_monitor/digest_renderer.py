from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from html import escape

from crypto_monitor.models import Digest, ProcessedArticle, TelegramArticleBlock
from crypto_monitor.normalization import format_article_date

SECTION_RULES = OrderedDict(
    [
        (
            "Регулирование Республики Казахстан",
            lambda a: a.geo_priority == 1 and _has(a, "regulation", "licensing"),
        ),
        (
            "Регулирование СНГ и Центральной Азии",
            lambda a: a.geo_priority == 2 and _has(a, "regulation", "licensing"),
        ),
        (
            "CBDC и государственные цифровые инициативы",
            lambda a: _has(a, "cbdc"),
        ),
        ("Банки и финтех", lambda a: _has(a, "banks")),
        (
            "Биржи, продукты, лицензирование",
            lambda a: _has(a, "exchanges", "products", "licensing"),
        ),
        (
            "Технологии, инфраструктура, безопасность",
            lambda a: _has(
                a,
                "blockchain-platforms",
                "wallets",
                "security-incidents",
                "ai-in-crypto",
                "tokenization",
                "defi",
                "stablecoins",
            ),
        ),
        (
            "Кратко: международные новости",
            lambda a: a.geo_priority == 3 and (a.priority in {"high", "critical"}),
        ),
    ]
)

PRIORITY_MARKERS = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}
PRIORITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
FOOTER_TEXT = (
    "Сводка сформирована автоматически. "
    "Возможны неточности в кратком изложении; "
    "для принятия решений обращайтесь к оригиналу."
)


def render_digest_locally(
    articles: list[ProcessedArticle],
    digest_date: str,
    max_items_per_section: int = 5,
    total_max_items: int = 25,
) -> Digest:
    selected = sorted(articles, key=_article_sort_key)[:total_max_items]
    sections: OrderedDict[str, list[ProcessedArticle]] = OrderedDict(
        (name, []) for name in SECTION_RULES
    )
    used_ids: set[str] = set()

    for name, predicate in SECTION_RULES.items():
        for article in selected:
            if article.id in used_ids:
                continue
            if predicate(article):
                sections[name].append(article)
                used_ids.add(article.id)
            if len(sections[name]) >= max_items_per_section:
                break

    sections = OrderedDict((name, items) for name, items in sections.items() if items)
    html = _render_html(sections, digest_date)
    plain_text = _render_plain(sections, digest_date)
    telegram_segments = _segment_telegram(_render_telegram_blocks(sections, digest_date))
    telegram_articles = _build_telegram_articles(sections)
    header_text = _build_header_text(digest_date, sections)
    footer_text = FOOTER_TEXT
    return Digest(
        digest_date=digest_date,
        html=html,
        plain_text=plain_text,
        telegram_segments=telegram_segments,
        telegram_articles=telegram_articles,
        header_text=header_text,
        footer_text=footer_text,
        stats={
            "total_articles": sum(len(items) for items in sections.values()),
            "by_section": {name: len(items) for name, items in sections.items()},
            "with_image": sum(
                1 for items in sections.values() for article in items if article.image_url
            ),
            "renderer": "local_fallback",
        },
    )


def _build_telegram_articles(
    sections: OrderedDict[str, list[ProcessedArticle]],
) -> list[TelegramArticleBlock]:
    blocks: list[TelegramArticleBlock] = []
    for section, articles in sections.items():
        for article in articles:
            blocks.append(
                TelegramArticleBlock(
                    section=section,
                    title=article.title_ru or article.title,
                    summary=article.summary or article.body[:600],
                    source_name=article.source_name,
                    source_url=article.source_url,
                    published_at_text=format_article_date(article.published_at),
                    priority=(article.priority or "medium").lower(),
                    image_url=article.image_url,
                )
            )
    return blocks


def _build_header_text(
    digest_date: str,
    sections: OrderedDict[str, list[ProcessedArticle]],
) -> str:
    total = sum(len(items) for items in sections.values())
    section_lines = [
        f"• {section}: {len(items)}" for section, items in sections.items() if items
    ]
    body = "\n".join(section_lines) or "• публикаций нет"
    return (
        f"Цифровые активы: {_display_date(digest_date)}\n"
        f"Публикаций в сводке: {total}\n"
        "\n"
        f"{body}"
    )


def _has(article: ProcessedArticle, *topics: str) -> bool:
    return any(topic in article.topics for topic in topics)


def _render_html(sections: OrderedDict[str, list[ProcessedArticle]], digest_date: str) -> str:
    total_articles = sum(len(items) for items in sections.values())
    display_date = _display_date(digest_date)
    body = [
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>",
        '<body style="font-family:Arial,sans-serif;'
        'background:#F4F6F8;margin:0;padding:24px;">',
        '<main style="max-width:640px;margin:0 auto;background:#fff;padding:24px;">',
        f"<h1 style=\"color:#1F4E79;\">Цифровые активы: {escape(display_date)}</h1>",
        f'<p style="color:#666;font-size:13px;">'
        f"Период покрытия: {escape(digest_date)} · {total_articles} публикаций</p>",
    ]
    for section, articles in sections.items():
        body.append(
            f'<h2 style="color:#1F4E79;border-bottom:1px solid #E4E7EB;">'
            f"{escape(section)}</h2>"
        )
        for article in articles:
            body.append(
                '<article style="border-left:4px solid #2E75B6;'
                'padding:12px;margin:12px 0;background:#F5F9FD;">'
                f"<strong>{escape((article.priority or 'medium').upper())}</strong>"
                f"<h3>{escape(article.title_ru or article.title)}</h3>"
                f"<p>{escape(article.summary or article.body[:500])}</p>"
                f'<p style="font-size:12px;color:#666;">'
                f"Источник: {escape(article.source_name)} "
                f"| Дата: {escape(format_article_date(article.published_at))} "
                f"| <a href=\"{escape(article.source_url)}\">Читать оригинал</a></p>"
                "</article>"
            )
    body.append(
        "<footer style=\"font-size:12px;color:#666;\">"
        f"{escape(FOOTER_TEXT)}"
        "</footer></main></body></html>"
    )
    return "".join(body)


def _render_plain(sections: OrderedDict[str, list[ProcessedArticle]], digest_date: str) -> str:
    lines = [f"Цифровые активы: {_display_date(digest_date)}", ""]
    for section, articles in sections.items():
        lines.extend([section, "-" * len(section)])
        for article in articles:
            lines.append(
                f"[{(article.priority or 'medium').upper()}] "
                f"{article.title_ru or article.title}"
            )
            lines.append(article.summary or article.body[:500])
            lines.append(
                f"Источник: {article.source_name} | "
                f"Дата: {format_article_date(article.published_at)} | "
                f"{article.source_url}"
            )
            lines.append("")
    lines.append(FOOTER_TEXT)
    return "\n".join(lines).strip()


def _render_telegram_blocks(
    sections: OrderedDict[str, list[ProcessedArticle]],
    digest_date: str,
) -> list[str]:
    blocks = [f"*Цифровые активы: {_escape_md(_display_date(digest_date))}*"]
    for section, articles in sections.items():
        blocks.append(f"\n*{_escape_md(section)}*")
        for article in articles:
            title = _escape_md(article.title_ru or article.title)
            summary = _escape_md(article.summary or article.body[:500])
            source = _escape_md(article.source_name)
            published = _escape_md(format_article_date(article.published_at))
            marker = PRIORITY_MARKERS.get(article.priority or "medium", "MEDIUM")
            blocks.append(
                f"*{marker}*\n*{title}*\n\n{summary}\n\n"
                f"{source} \\| {published} \\| [оригинал]({article.source_url})\n"
            )
    blocks.append(f"\n_{_escape_md(FOOTER_TEXT)}_")
    return blocks


def _segment_telegram(blocks: list[str], limit: int = 4000) -> list[str]:
    segments: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            segments.append(current + "\n\n_продолжение далее\\.\\.\\._")
        current = block
    if current:
        segments.append(current)
    return segments or ["Сводка пуста"]


def _escape_md(value: str) -> str:
    return "".join(f"\\{char}" if char in r"_*[]()~`>#+-=|{}.!" else char for char in value)


def _article_sort_key(article: ProcessedArticle) -> tuple[int, int, int, float]:
    geo_priority = article.geo_priority if article.geo_priority in {1, 2, 3} else 4
    published = article.published_at.timestamp() if article.published_at else 0.0
    return (
        geo_priority,
        -PRIORITY_RANK.get(article.priority or "medium", 2),
        -(article.score or 0),
        -published,
    )


def parse_date(value: str | None) -> str:
    if not value:
        return datetime.now().date().isoformat()
    return value


def _display_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value
