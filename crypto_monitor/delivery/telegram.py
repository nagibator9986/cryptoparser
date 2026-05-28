from __future__ import annotations

import re

import httpx

from crypto_monitor.config import Settings
from crypto_monitor.models import Digest
from crypto_monitor.retry import retry_call


class TelegramDelivery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        for segment in split_telegram_segments(digest.telegram_segments):
            current_segment = segment

            try:
                retry_call(
                    lambda text=current_segment: self._send_message(
                        url=url,
                        chat_id=target_chat_id,
                        text=text,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=disable_web_page_preview,
                    ),
                    attempts=3,
                    base_delay_seconds=2.0,
                    retry_exceptions=(httpx.HTTPError,),
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                retry_call(
                    lambda text=current_segment: self._send_message(
                        url=url,
                        chat_id=target_chat_id,
                        text=unescape_markdown_v2(text),
                        parse_mode=None,
                        disable_web_page_preview=disable_web_page_preview,
                    ),
                    attempts=3,
                    base_delay_seconds=2.0,
                    retry_exceptions=(httpx.HTTPError,),
                )

    @staticmethod
    def _send_message(
        *,
        url: str,
        chat_id: str,
        text: str,
        parse_mode: str | None,
        disable_web_page_preview: bool,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = httpx.post(url, json=payload, timeout=30)
        response.raise_for_status()


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
