from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from html import escape

from crypto_monitor.models import Digest, ProcessedArticle, TelegramArticleBlock
from crypto_monitor.normalization import format_article_date

# Articles with topics containing 'events' are dedicated to the Events
# section regardless of which other tags they carry. We bake the exclusion
# directly into the regulation/licensing predicates so that a forum
# announcement with `topics=['events', 'regulation']` reaches the Events
# section even though the SECTION_RULES iteration order places Events last
# (matching the SKILL.md render order).
SECTION_RULES = OrderedDict(
    [
        (
            "Законодательные изменения (РК и СНГ)",
            lambda a: a.is_legislative and a.geo_priority in {1, 2},
        ),
        (
            "Регулирование Республики Казахстан",
            lambda a: a.geo_priority == 1
            and _has(a, "regulation", "licensing")
            and not _has(a, "events"),
        ),
        (
            "Регулирование СНГ и Центральной Азии",
            lambda a: a.geo_priority == 2
            and _has(a, "regulation", "licensing")
            and not _has(a, "events"),
        ),
        (
            "CBDC и государственные цифровые инициативы",
            lambda a: _has(a, "cbdc") and not _has(a, "events"),
        ),
        ("Банки и финтех", lambda a: _has(a, "banks") and not _has(a, "events")),
        (
            "Биржи, продукты, лицензирование",
            lambda a: _has(a, "exchanges", "products", "licensing")
            and not _has(a, "events"),
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
            )
            and not _has(a, "events"),
        ),
        (
            "Кратко: международные новости",
            lambda a: a.geo_priority == 3
            and (a.priority in {"high", "critical"})
            and not _has(a, "events"),
        ),
        (
            "Мероприятия и форумы",
            lambda a: _has(a, "events") and (a.event_scale or "minor") != "minor",
        ),
    ]
)

LEGISLATIVE_STAGE_RU = {
    "introduced": "внесён законопроект",
    "debated": "рассматривается",
    "adopted": "принят",
    "signed": "подписан",
    "in_force": "вступил в силу",
}

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

    # Section-specific resort: legislation goes by stage (signed > adopted >
    # debated > introduced > in_force), events by scale (kz > cis > global).
    leg_section = "Законодательные изменения (РК и СНГ)"
    if leg_section in sections:
        sections[leg_section].sort(key=_legislation_sort_key)
    events_section = "Мероприятия и форумы"
    if events_section in sections:
        sections[events_section].sort(key=_events_sort_key)

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
            stage_ru = (
                LEGISLATIVE_STAGE_RU.get(article.legislative_stage)
                if article.legislative_stage
                else None
            )
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
                    event_date=article.event_date,
                    event_location=article.event_location,
                    legislative_stage=stage_ru,
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
            title = article.title_ru or article.title
            if article.is_legislative and article.legislative_stage:
                stage_ru = LEGISLATIVE_STAGE_RU.get(article.legislative_stage)
                if stage_ru:
                    title = f"{stage_ru[0].upper()}{stage_ru[1:]}: {title}"
            meta_lines: list[str] = []
            if article.event_date:
                meta_lines.append(f"Дата мероприятия: {escape(article.event_date)}")
            if article.event_location:
                meta_lines.append(f"Место: {escape(article.event_location)}")
            meta_html = (
                "<p style=\"font-size:12px;color:#444;\">"
                + " · ".join(meta_lines)
                + "</p>"
                if meta_lines
                else ""
            )
            body.append(
                '<article style="border-left:4px solid #2E75B6;'
                'padding:12px;margin:12px 0;background:#F5F9FD;">'
                f"<strong>{escape((article.priority or 'medium').upper())}</strong>"
                f"<h3>{escape(title)}</h3>"
                f"{meta_html}"
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
            title = article.title_ru or article.title
            if article.is_legislative and article.legislative_stage:
                stage_ru = LEGISLATIVE_STAGE_RU.get(article.legislative_stage)
                if stage_ru:
                    title = f"{stage_ru[0].upper()}{stage_ru[1:]}: {title}"
            lines.append(f"[{(article.priority or 'medium').upper()}] {title}")
            if article.event_date or article.event_location:
                meta = []
                if article.event_date:
                    meta.append(f"Дата мероприятия: {article.event_date}")
                if article.event_location:
                    meta.append(f"Место: {article.event_location}")
                lines.append(" · ".join(meta))
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
            base_title = article.title_ru or article.title
            if article.is_legislative and article.legislative_stage:
                stage_ru = LEGISLATIVE_STAGE_RU.get(article.legislative_stage)
                if stage_ru:
                    base_title = f"{stage_ru[0].upper()}{stage_ru[1:]}: {base_title}"
            title = _escape_md(base_title)
            summary = _escape_md(article.summary or article.body[:500])
            source = _escape_md(article.source_name)
            published = _escape_md(format_article_date(article.published_at))
            marker = PRIORITY_MARKERS.get(article.priority or "medium", "MEDIUM")
            meta_line = ""
            if article.event_date or article.event_location:
                parts: list[str] = []
                if article.event_date:
                    parts.append(_escape_md(f"Дата: {article.event_date}"))
                if article.event_location:
                    parts.append(_escape_md(f"Место: {article.event_location}"))
                meta_line = " \\| ".join(parts) + "\n\n"
            blocks.append(
                f"*{marker}*\n*{title}*\n\n{meta_line}{summary}\n\n"
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


_LEGISLATIVE_STAGE_RANK = {
    "signed": 0,
    "adopted": 1,
    "debated": 2,
    "introduced": 3,
    "in_force": 4,
    None: 5,
}
_EVENT_SCALE_RANK = {
    "kz_major": 0,
    "cis_major": 1,
    "global_major": 2,
    "minor": 3,
    None: 4,
}


def _legislation_sort_key(article: ProcessedArticle) -> tuple[int, int, int]:
    geo = article.geo_priority if article.geo_priority in {1, 2} else 3
    stage_rank = _LEGISLATIVE_STAGE_RANK.get(article.legislative_stage, 5)
    return (geo, stage_rank, -(article.score or 0))


def _events_sort_key(article: ProcessedArticle) -> tuple[int, int]:
    scale_rank = _EVENT_SCALE_RANK.get(article.event_scale, 4)
    return (scale_rank, -(article.score or 0))


def parse_date(value: str | None) -> str:
    if not value:
        return datetime.now().date().isoformat()
    return value


def _display_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value
