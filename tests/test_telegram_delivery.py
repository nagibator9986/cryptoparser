from crypto_monitor.delivery.telegram import (
    PHOTO_CAPTION_LIMIT,
    escape_md,
    render_article_caption,
    split_telegram_segments,
    unescape_markdown_v2,
)
from crypto_monitor.models import TelegramArticleBlock


def test_unescape_markdown_v2() -> None:
    assert unescape_markdown_v2(r"\*Title\* link\.") == "*Title* link."


def test_split_telegram_segments_keeps_limit() -> None:
    chunks = split_telegram_segments(["a" * 10], limit=4)
    assert chunks == ["aaaa", "aaaa", "aa"]
    assert all(len(chunk) <= 4 for chunk in chunks)


def test_escape_md_escapes_all_markdown_v2_specials() -> None:
    assert escape_md("Title (v1.0) - hot!") == r"Title \(v1\.0\) \- hot\!"


def test_render_article_caption_fits_photo_limit_and_includes_link() -> None:
    block = TelegramArticleBlock(
        section="Регулирование РК",
        title="AFSA выдало лицензию криптопровайдеру",
        summary=(
            "AFSA в МФЦА выдало лицензию на хранение цифровых активов. "
            "Лицензия сопоставима со стандартами MiCA."
        ) * 30,
        source_name="AFSA",
        source_url="https://afsa.aifc.kz/news/1",
        published_at_text="26.05.2026 09:00",
        priority="high",
        image_url="https://afsa.aifc.kz/img.jpg",
    )

    caption = render_article_caption(block)

    assert len(caption) <= PHOTO_CAPTION_LIMIT
    assert "AFSA" in caption
    assert "https://afsa.aifc.kz/news/1" in caption
    assert "*HIGH*" in caption
