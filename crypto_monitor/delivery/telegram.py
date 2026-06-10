from __future__ import annotations

import logging
import re

import httpx

from crypto_monitor.config import Settings
from crypto_monitor.models import CryptoRatesSnapshot, Digest, TelegramArticleBlock
from crypto_monitor.rates import (
    RATES_ATTRIBUTION,
    RATES_TITLE,
    display_date,
    format_amount,
)
from crypto_monitor.retry import retry_call

logger = logging.getLogger(__name__)

PRIORITY_LABELS = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}
TELEGRAM_MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
PHOTO_CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


class TelegramDelivery:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def configured(self, chat_id: str | None = None) -> bool:
        target_chat_id = chat_id or self.settings.telegram_chat_id
        return bool(self.settings.telegram_bot_token and target_chat_id)

    def send(
        self,
        digest: Digest,
        chat_id: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        target_chat_id = chat_id or self.settings.telegram_chat_id
        if not self.configured(target_chat_id):
            raise RuntimeError("Telegram is not configured")
        assert self.settings.telegram_bot_token
        assert target_chat_id

        # Prefer the structured article path: one Telegram message per
        # article, with the article's image attached as a photo when
        # present. This matches how editorial digests are read on phones
        # and lets us keep MarkdownV2 captions per item.
        if digest.telegram_articles:
            self._send_structured(
                digest,
                target_chat_id,
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        self._send_legacy_segments(
            digest.telegram_segments,
            target_chat_id,
            disable_web_page_preview=disable_web_page_preview,
        )

    def send_rates(
        self,
        snapshot: CryptoRatesSnapshot,
        chat_id: str | None = None,
    ) -> None:
        target_chat_id = chat_id or self.settings.telegram_chat_id
        if not self.configured(target_chat_id):
            raise RuntimeError("Telegram is not configured")
        token = self.settings.telegram_bot_token
        assert token
        assert target_chat_id
        try:
            self._send_text(
                token,
                target_chat_id,
                render_rates_markdown_v2(snapshot),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                retry=False,
            )
        except httpx.HTTPStatusError as exc:
            # A MarkdownV2 escaping miss surfaces as 400; resend the plain
            # text so the rates still reach the group.
            if exc.response.status_code != 400:
                raise
            from crypto_monitor.rates import render_rates_plain

            self._send_text(
                token,
                target_chat_id,
                render_rates_plain(snapshot),
                parse_mode=None,
                disable_web_page_preview=True,
            )

    def _send_structured(
        self,
        digest: Digest,
        chat_id: str,
        *,
        disable_web_page_preview: bool,
    ) -> None:
        token = self.settings.telegram_bot_token
        assert token

        if digest.header_text:
            self._send_text(
                token,
                chat_id,
                header_markdown_v2(digest),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )

        current_section = ""
        for article in digest.telegram_articles:
            if article.section != current_section:
                current_section = article.section
                self._send_text(
                    token,
                    chat_id,
                    f"*{escape_md(current_section)}*",
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            self._send_article(
                token,
                chat_id,
                article,
                disable_web_page_preview=disable_web_page_preview,
            )

        if digest.footer_text:
            self._send_text(
                token,
                chat_id,
                f"_{escape_md(digest.footer_text)}_",
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )

    def _send_article(
        self,
        token: str,
        chat_id: str,
        article: TelegramArticleBlock,
        *,
        disable_web_page_preview: bool,
    ) -> None:
        if article.image_url:
            caption = render_article_caption(article, limit=PHOTO_CAPTION_LIMIT)
            try:
                self._send_photo(
                    token,
                    chat_id,
                    photo=article.image_url,
                    caption=caption,
                    parse_mode="MarkdownV2",
                )
                return
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "telegram_send_photo_failed status=%s url=%s falling_back_to_text",
                    exc.response.status_code,
                    article.image_url,
                )
        text = render_article_text(article, limit=MESSAGE_LIMIT)
        self._send_text(
            token,
            chat_id,
            text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=disable_web_page_preview,
        )

    def _send_legacy_segments(
        self,
        segments: list[str],
        chat_id: str,
        *,
        disable_web_page_preview: bool,
    ) -> None:
        token = self.settings.telegram_bot_token
        assert token
        for segment in split_telegram_segments(segments):
            current_segment = segment
            try:
                retry_call(
                    lambda text=current_segment: self._send_text(
                        token,
                        chat_id,
                        text,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=disable_web_page_preview,
                    ),
                    attempts=3,
                    base_delay_seconds=2.0,
                    retry_exceptions=(httpx.HTTPError,),
                    delay_for_exception=telegram_retry_after,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                retry_call(
                    lambda text=current_segment: self._send_text(
                        token,
                        chat_id,
                        unescape_markdown_v2(text),
                        parse_mode=None,
                        disable_web_page_preview=disable_web_page_preview,
                    ),
                    attempts=3,
                    base_delay_seconds=2.0,
                    retry_exceptions=(httpx.HTTPError,),
                    delay_for_exception=telegram_retry_after,
                )

    def _send_text(
        self,
        token: str,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None,
        disable_web_page_preview: bool,
        retry: bool = True,
    ) -> None:
        def attempt() -> None:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_web_page_preview,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            response = self._client.post(url, json=payload)
            response.raise_for_status()

        if not retry:
            # A MarkdownV2 escaping miss yields a deterministic 400; retrying it
            # only delays the plain-text fallback. Caller handles the error.
            attempt()
            return
        retry_call(
            attempt,
            attempts=3,
            base_delay_seconds=2.0,
            retry_exceptions=(httpx.HTTPError,),
            delay_for_exception=telegram_retry_after,
        )

    def _send_photo(
        self,
        token: str,
        chat_id: str,
        *,
        photo: str,
        caption: str,
        parse_mode: str | None,
    ) -> None:
        def attempt() -> None:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            response = self._client.post(url, json=payload)
            response.raise_for_status()

        retry_call(
            attempt,
            attempts=3,
            base_delay_seconds=2.0,
            retry_exceptions=(httpx.HTTPError,),
            delay_for_exception=telegram_retry_after,
        )


def render_rates_markdown_v2(snapshot: CryptoRatesSnapshot) -> str:
    header = f"*{escape_md(RATES_TITLE)}*"
    subheader = f"_{escape_md('за ' + display_date(snapshot.date))}_"
    lines = [header, subheader, ""]
    for rate in snapshot.rates:
        kzt = escape_md(f"{format_amount(rate.price_kzt)} ₸")
        usd = (
            f" \\| {escape_md('$' + format_amount(rate.price_usd))}"
            if rate.price_usd is not None
            else ""
        )
        symbol = escape_md(rate.symbol)
        name = escape_md(rate.name)
        lines.append(f"`{symbol}` {name} — {kzt}{usd}")
    lines.append("")
    lines.append(f"_{escape_md(RATES_ATTRIBUTION)}_")
    lines.append(f"[{escape_md('Источник: КГД / qoldau.kz')}]({snapshot.source_url})")
    return "\n".join(lines)


def telegram_retry_after(exc: Exception) -> float | None:
    """Extract a Telegram 429 ``retry_after`` delay from an HTTP error.

    Telegram returns the wait time in the JSON body
    (``parameters.retry_after``) and/or the ``Retry-After`` header. Honour it
    so we back off exactly as long as the API asks instead of guessing.
    """

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = exc.response
    if response.status_code != 429:
        return None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        retry_after = body.get("parameters", {}).get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return float(retry_after)
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None


def escape_md(value: str) -> str:
    return "".join(f"\\{ch}" if ch in TELEGRAM_MD2_SPECIAL else ch for ch in value)


def header_markdown_v2(digest: Digest) -> str:
    header = digest.header_text or f"Crypto Monitor — {digest.digest_date}"
    return escape_md(header)


def render_article_caption(
    article: TelegramArticleBlock,
    *,
    limit: int = PHOTO_CAPTION_LIMIT,
) -> str:
    return _render_article_body(article, limit=limit)


def render_article_text(
    article: TelegramArticleBlock,
    *,
    limit: int = MESSAGE_LIMIT,
) -> str:
    return _render_article_body(article, limit=limit)


def _render_article_body(article: TelegramArticleBlock, *, limit: int) -> str:
    label = PRIORITY_LABELS.get(article.priority, "MEDIUM")
    title = escape_md(article.title)
    source = escape_md(article.source_name)
    published = escape_md(article.published_at_text)
    link_text = escape_md("оригинал")
    meta = f"{source} \\| {published} \\| [{link_text}]({article.source_url})"

    overhead = (
        len(escape_md(label)) + len(title) + len(meta) + 6
    )  # newlines and bold markers
    summary_budget = max(0, limit - overhead)
    summary = _truncate_words(article.summary or "", summary_budget)
    safe_summary = escape_md(summary)

    parts = [f"*{escape_md(label)}*", f"*{title}*"]
    if safe_summary:
        parts.append(safe_summary)
    parts.append(meta)
    rendered = "\n\n".join(parts)
    if len(rendered) > limit:
        rendered = rendered[: limit - 3] + "..."
    return rendered


def _truncate_words(text: str, char_budget: int) -> str:
    text = text.strip()
    if char_budget <= 0:
        return ""
    # Reserve some space because escaping MarkdownV2 inflates length ~20-30%.
    safe_budget = max(40, int(char_budget * 0.7))
    if len(text) <= safe_budget:
        return text
    cut = text[:safe_budget]
    space = cut.rfind(" ")
    if space > safe_budget * 0.5:
        cut = cut[:space]
    return cut.rstrip() + "..."


def unescape_markdown_v2(value: str) -> str:
    return re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", value)


def split_telegram_segments(segments: list[str], limit: int = 4096) -> list[str]:
    result: list[str] = []
    for segment in segments:
        if len(segment) <= limit:
            result.append(segment)
            continue

        current = ""
        for line in segment.splitlines(keepends=True):
            if len(line) > limit:
                if current:
                    result.append(current.rstrip())
                    current = ""
                result.extend(
                    line[index : index + limit] for index in range(0, len(line), limit)
                )
                continue

            candidate = f"{current}{line}"
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    result.append(current.rstrip())
                current = line
        if current:
            result.append(current.rstrip())
    return result
