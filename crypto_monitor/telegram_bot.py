from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from crypto_monitor.collector_runner import CollectorRunner
from crypto_monitor.config import Settings
from crypto_monitor.delivery.telegram import TelegramDelivery
from crypto_monitor.digest_renderer import render_digest_locally
from crypto_monitor.gemini import DryRunLlmClient, GeminiClient
from crypto_monitor.models import (
    WEEKDAY_NAMES,
    Digest,
    ProcessedArticle,
    QaResult,
    RawArticle,
    SourceConfig,
    TelegramChatSettings,
)
from crypto_monitor.normalization import (
    digest_date_or_previous_day,
    is_within_schedule_window,
    zoneinfo_or_utc,
)
from crypto_monitor.pipeline import GeminiSkillPipeline
from crypto_monitor.skills import SkillLoader
from crypto_monitor.sources import load_sources
from crypto_monitor.storage import SqliteStorage

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
PriorityName = Literal["low", "medium", "high", "critical"]
TRUE_VALUES = {"1", "true", "yes", "on", "да", "вкл", "enable", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "нет", "выкл", "disable", "disabled"}

COMMAND_ALIASES = {
    "cm_start": "crypto_start",
    "cm_help": "crypto_help",
    "cm_settings": "crypto_settings",
    "cm_set": "crypto_set",
    "cm_unset": "crypto_unset",
    "cm_sources": "crypto_sources",
    "cm_collect": "crypto_collect",
    "cm_process": "crypto_process",
    "cm_digest": "crypto_digest",
    "cm_latest": "crypto_latest",
    "cm_run": "crypto_run",
    "cm_search": "crypto_search",
    "cm_schedule": "crypto_schedule",
}

READ_ONLY_COMMANDS = {"crypto_help", "crypto_settings", "crypto_search"}

HELP_TEXT = """Crypto Monitor commands
/crypto_start - register this group for digests
/crypto_settings - show current group settings
/crypto_set <key> <value> - change one setting
/crypto_schedule [HH:MM] [days] - show or change delivery schedule
/crypto_unset <key> - reset sources, min_priority, or last_sent
/crypto_sources - show source catalog and current selection
/crypto_sources all - use all enabled sources
/crypto_sources id1,id2 - use selected source ids
/crypto_collect - collect articles from selected enabled sources
/crypto_process - process stored raw articles
/crypto_digest [YYYY-MM-DD] - build and send a new digest
/crypto_latest [YYYY-MM-DD] - send an archived digest
/crypto_run [YYYY-MM-DD] - collect, process, build, and send
/crypto_search <query> - search processed articles and digests

Settings keys: timezone, digest_time, digest_limit, section_limit,
total_limit, min_priority, dry_run, previews, delivery, auto_collect,
auto_process, weekdays.

Only group administrators can change settings or run pipeline commands."""


class TelegramApiError(RuntimeError):
    """Raised when Telegram Bot API returns ok=false."""


class TelegramConflictError(TelegramApiError):
    """Raised when another getUpdates request is active for the same bot token."""


class TelegramBotApi:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        request_timeout: float | None = None,
    ) -> Any:
        try:
            response = httpx.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=request_timeout or self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            description = _telegram_error_description(exc.response)
            if status_code == 409:
                raise TelegramConflictError(description) from None
            raise TelegramApiError(f"HTTP {status_code}: {description}") from None
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"{type(exc).__name__}: {exc}") from None

        data = response.json()
        if not data.get("ok", False):
            description = data.get("description") or "Telegram API returned ok=false"
            if data.get("error_code") == 409:
                raise TelegramConflictError(str(description))
            raise TelegramApiError(str(description))
        return data.get("result")

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.request("getUpdates", payload, request_timeout=timeout + 15)
        return result if isinstance(result, list) else []

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        result = self.request(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
        )
        return bool(result)

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        result = self.request("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        return result if isinstance(result, dict) else {}

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
        reply_to_message_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        self.request("sendMessage", payload)


class TelegramCommandBot:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: SqliteStorage,
        api: TelegramBotApi | None = None,
        collector_runner: CollectorRunner | None = None,
        pipeline_factory: Callable[[bool], GeminiSkillPipeline] | None = None,
        admin_checker: Callable[[str, int], bool] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if api is None:
            if not settings.telegram_bot_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is required for TelegramCommandBot")
            api = TelegramBotApi(settings.telegram_bot_token)
        self.settings = settings
        self.storage = storage
        self.api = api
        self.collector_runner = collector_runner or CollectorRunner()
        self.pipeline_factory = pipeline_factory or self._default_pipeline_factory
        self.admin_checker = admin_checker
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def prepare_long_polling(self) -> None:
        if hasattr(self.api, "delete_webhook"):
            try:
                self.api.delete_webhook(drop_pending_updates=False)
            except Exception as exc:
                logger.warning(
                    "telegram_delete_webhook_failed error=%s: %s",
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.info("telegram_webhook_deleted_for_long_polling")

    def poll_once(self, offset: int | None = None, timeout: int = 30) -> int | None:
        next_offset = offset
        for update in self.api.get_updates(offset=offset, timeout=timeout):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
            self.handle_update(update)
        self.run_scheduled_jobs()
        return next_offset

    def run_forever(self, poll_timeout: int = 30, poll_interval: float = 1.0) -> None:
        self.prepare_long_polling()
        offset: int | None = None
        while True:
            try:
                offset = self.poll_once(offset=offset, timeout=poll_timeout)
            except KeyboardInterrupt:
                raise
            except TelegramConflictError as exc:
                logger.warning(
                    "telegram_polling_conflict: %s. "
                    "Only one Railway replica/service/local bot can use this token.",
                    exc,
                )
                time.sleep(max(poll_interval, 10.0))
                continue
            except TelegramApiError as exc:
                logger.error("telegram_polling_api_error: %s", exc)
            except Exception:
                logger.exception("telegram_polling_failed")
            time.sleep(poll_interval)

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text") or "").strip()
        parsed = parse_command(text)
        if not parsed:
            return

        command, args = parsed
        command = COMMAND_ALIASES.get(command, command)
        if not command.startswith("crypto_"):
            return

        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return
        chat_title = _chat_title(chat)
        chat_type = str(chat.get("type") or "")
        user = message.get("from") or {}
        user_id = user.get("id")
        reply_to_message_id = message.get("message_id")

        if command not in READ_ONLY_COMMANDS and not self._is_authorized(
            chat_id,
            int(user_id or 0),
            chat_type,
        ):
            message = (
                "Команды настройки и запуска доступны "
                "только администраторам группы."
            )
            self._send_plain(
                chat_id,
                message,
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            response = self._dispatch(command, args, chat_id, chat_title)
        except Exception as exc:
            logger.exception(
                "telegram_command_failed command=%s chat_id=%s",
                command,
                chat_id,
            )
            response = f"Ошибка: {type(exc).__name__}: {exc}"

        if response:
            self._send_plain(chat_id, response, reply_to_message_id=reply_to_message_id)

    def run_scheduled_jobs(self) -> int:
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        sent = 0
        for chat_settings in self.storage.list_telegram_chat_settings(only_enabled=True):
            digest_date = self._default_digest_date_for_chat(chat_settings, now=now)
            if not self._is_delivery_weekday(chat_settings, now):
                continue
            if not is_within_schedule_window(
                now,
                chat_settings.digest_time,
                chat_settings.timezone,
            ):
                continue
            if chat_settings.last_digest_sent_date == digest_date:
                continue
            try:
                if chat_settings.auto_collect:
                    self._collect_for_chat(chat_settings)
                if chat_settings.auto_process:
                    self._process_for_chat(chat_settings)
                digest, qa = self._build_digest_for_chat(chat_settings, digest_date)
                if qa.recommendation == "do_not_send":
                    text = (
                        f"Плановая сводка за {digest_date} "
                        f"заблокирована QA: {qa.severity}"
                    )
                    self._send_plain(
                        chat_settings.chat_id,
                        text,
                    )
                else:
                    self._send_digest(chat_settings, digest)
                    sent += 1
                chat_settings.last_digest_sent_date = digest_date
                self.storage.save_telegram_chat_settings(chat_settings)
            except Exception:
                logger.exception(
                    "scheduled_telegram_digest_failed chat_id=%s",
                    chat_settings.chat_id,
                )
                self._send_plain(
                    chat_settings.chat_id,
                    f"Не удалось отправить плановую сводку за {digest_date}.",
                )
        return sent

    def _dispatch(
        self,
        command: str,
        args: str,
        chat_id: str,
        chat_title: str | None,
    ) -> str | None:
        if command == "crypto_help":
            return HELP_TEXT
        if command == "crypto_start":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return "Группа подключена.\n\n" + format_chat_settings(chat_settings)
        if command == "crypto_settings":
            chat_settings = self.storage.load_telegram_chat_settings(chat_id)
            if not chat_settings:
                return (
                    "Группа еще не подключена. "
                    "Администратор может выполнить /crypto_start."
                )
            return format_chat_settings(chat_settings)
        if command == "crypto_set":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._set_command(chat_settings, args)
        if command == "crypto_schedule":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._schedule_command(chat_settings, args)
        if command == "crypto_unset":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._unset_command(chat_settings, args)
        if command == "crypto_sources":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._sources_command(chat_settings, args)
        if command == "crypto_collect":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._collect_for_chat(chat_settings)
        if command == "crypto_process":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._process_for_chat(chat_settings)
        if command == "crypto_digest":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._digest_command(chat_settings, args)
        if command == "crypto_latest":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            return self._latest_command(chat_settings, args)
        if command == "crypto_search":
            return self._search_command(args)
        if command == "crypto_run":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            collect_result = self._collect_for_chat(chat_settings)
            process_result = self._process_for_chat(chat_settings)
            digest_result = self._digest_command(chat_settings, args)
            return "\n".join([collect_result, process_result, digest_result])
        return None

    def _set_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        key, value = _split_key_value(args)
        normalized = _normalize_key(key)

        if normalized == "timezone":
            ZoneInfo(value)
            chat_settings.timezone = value
        elif normalized == "digest_time":
            chat_settings.digest_time = _normalize_time(value)
        elif normalized == "weekdays":
            chat_settings.digest_weekdays = parse_weekdays(value)
        elif normalized == "digest_limit":
            chat_settings.digest_limit = _parse_int(value, minimum=1, maximum=100)
        elif normalized == "section_limit":
            chat_settings.max_items_per_section = _parse_int(value, minimum=1, maximum=20)
        elif normalized == "total_limit":
            chat_settings.total_max_items = _parse_int(value, minimum=1, maximum=100)
        elif normalized == "min_priority":
            priority = value.lower()
            if priority not in PRIORITY_RANK:
                raise ValueError("min_priority must be one of: low, medium, high, critical")
            chat_settings.min_priority = cast(PriorityName, priority)
        elif normalized == "dry_run":
            chat_settings.dry_run = _parse_bool(value)
        elif normalized == "previews":
            chat_settings.disable_web_page_preview = not _parse_bool(value)
        elif normalized == "delivery":
            chat_settings.enabled = _parse_bool(value)
        elif normalized == "auto_collect":
            chat_settings.auto_collect = _parse_bool(value)
        elif normalized == "auto_process":
            chat_settings.auto_process = _parse_bool(value)
        else:
            raise ValueError(f"Unknown setting: {key}")

        _validate_chat_settings(chat_settings)
        self.storage.save_telegram_chat_settings(chat_settings)
        return "Настройка сохранена.\n\n" + format_chat_settings(chat_settings)

    def _unset_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        key = _normalize_key(args.strip())
        if key in {"sources", "source_ids"}:
            chat_settings.source_ids = []
        elif key in {"weekdays", "digest_weekdays"}:
            chat_settings.digest_weekdays = list(range(7))
        elif key == "min_priority":
            chat_settings.min_priority = "low"
        elif key in {"last_sent", "last_digest_sent_date"}:
            chat_settings.last_digest_sent_date = None
        else:
            raise ValueError("Can reset only: sources, min_priority, last_sent")
        self.storage.save_telegram_chat_settings(chat_settings)
        return "Настройка сброшена.\n\n" + format_chat_settings(chat_settings)

    def _schedule_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        raw_args = args.strip()
        if not raw_args:
            return format_schedule(chat_settings)

        parts = raw_args.split(maxsplit=1)
        first = parts[0]
        if ":" in first:
            chat_settings.digest_time = _normalize_time(first)
            if len(parts) > 1:
                chat_settings.digest_weekdays = parse_weekdays(parts[1])
        else:
            chat_settings.digest_weekdays = parse_weekdays(raw_args)

        self.storage.save_telegram_chat_settings(chat_settings)
        return "Расписание сохранено.\n\n" + format_schedule(chat_settings)

    def _sources_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        raw_args = args.strip()
        if raw_args:
            known_sources = {source.id for source in self._all_sources()}
            if raw_args.lower() in {"all", "*", "все"}:
                chat_settings.source_ids = []
            else:
                source_ids = [
                    item.strip()
                    for chunk in raw_args.split()
                    for item in chunk.split(",")
                    if item.strip()
                ]
                unknown = [
                    source_id for source_id in source_ids if source_id not in known_sources
                ]
                if unknown:
                    raise ValueError(f"Unknown source ids: {', '.join(unknown)}")
                chat_settings.source_ids = source_ids
            self.storage.save_telegram_chat_settings(chat_settings)

        all_sources = self._all_sources()
        selected = set(chat_settings.source_ids)
        current = (
            "all enabled sources" if not selected else ", ".join(chat_settings.source_ids)
        )
        lines = [f"Current source selection: {current}", "", "Source catalog:"]
        if not all_sources:
            lines.append("No sources configured in CRYPTO_MONITOR_SOURCES_FILE.")
            return "\n".join(lines)

        for source in all_sources:
            chosen = "selected" if not selected or source.id in selected else "off"
            enabled = "enabled" if source.enabled else "disabled"
            lines.append(
                f"- {source.id}: {source.name} ({source.type}, {enabled}, {chosen})"
            )
        return "\n".join(lines)

    def _collect_for_chat(self, chat_settings: TelegramChatSettings) -> str:
        sources = self._selected_enabled_sources(chat_settings)
        if not sources:
            return "Нет включенных источников для этой группы."

        limit = max(1, min(chat_settings.digest_limit, 50))
        articles = self.collector_runner.collect_all(
            sources,
            limit_per_source=limit,
            status_recorder=self.storage,
        )
        saved = self.storage.save_raw_articles(articles)
        self.storage.log_event(
            "telegram.collect",
            {
                "chat_id": chat_settings.chat_id,
                "collected": len(articles),
                "saved": saved,
                "sources": [source.id for source in sources],
            },
        )
        return f"Сбор завершен: получено {len(articles)}, новых {saved}."

    def _process_for_chat(self, chat_settings: TelegramChatSettings) -> str:
        raw_articles = self.storage.load_raw_articles(limit=chat_settings.digest_limit)
        raw_articles = self._filter_raw_articles(chat_settings, raw_articles)
        if not raw_articles:
            return "Нет raw-статей для обработки."

        pipeline = self.pipeline_factory(chat_settings.dry_run)
        processed = pipeline.process_articles(raw_articles)
        canonical = pipeline.deduplicate(processed)
        self.storage.save_processed_articles(canonical)
        self.storage.log_event(
            "telegram.process",
            {
                "chat_id": chat_settings.chat_id,
                "raw": len(raw_articles),
                "processed": len(processed),
                "canonical": len(canonical),
                "dry_run": chat_settings.dry_run,
            },
        )
        return (
            f"Обработка завершена: raw {len(raw_articles)}, "
            f"processed {len(processed)}, canonical {len(canonical)}."
        )

    def _digest_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        digest_date = args.strip() or None
        digest, qa = self._build_digest_for_chat(chat_settings, digest_date)
        if qa.recommendation == "do_not_send":
            return f"QA заблокировал отправку: severity={qa.severity}."
        self._send_digest(chat_settings, digest)
        return (
            f"Сводка за {digest.digest_date} отправлена. "
            f"QA={qa.recommendation}, severity={qa.severity}."
        )

    def _latest_command(self, chat_settings: TelegramChatSettings, args: str) -> str:
        digest_date = args.strip()
        if not digest_date:
            archived = self.storage.list_digests(limit=1)
            if not archived:
                return "В архиве пока нет сводок."
            digest_date = str(archived[0]["digest_date"])

        digest = self.storage.load_digest(digest_date)
        if not digest:
            return f"Сводка не найдена: {digest_date}."
        self._send_digest(chat_settings, digest)
        return f"Архивная сводка за {digest.digest_date} отправлена."

    def _search_command(self, args: str) -> str:
        query = args.strip()
        if not query:
            return "Использование: /crypto_search <текст запроса>"
        result = self.storage.search_archive(query, limit=5, kind="all")
        lines = [f"Результаты поиска: {query}"]
        processed = result.get("processed_articles", [])
        digests = result.get("digests", [])
        if processed:
            lines.append("\nПубликации:")
            for item in processed[:5]:
                lines.append(
                    f"- {item['title']} | {item['source_name']} | "
                    f"{item.get('priority') or '-'} | {item['source_url']}"
                )
        if digests:
            lines.append("\nСводки:")
            for item in digests[:5]:
                lines.append(f"- {item['digest_date']}")
        if len(lines) == 1:
            lines.append("Ничего не найдено.")
        return "\n".join(lines)

    def _build_digest_for_chat(
        self,
        chat_settings: TelegramChatSettings,
        digest_date: str | None,
    ) -> tuple[Digest, QaResult]:
        effective_date = digest_date or self._default_digest_date_for_chat(chat_settings)
        articles = self._load_processed_for_chat(chat_settings, effective_date)
        if not articles:
            digest = render_digest_locally(
                [],
                digest_date=effective_date,
                max_items_per_section=chat_settings.max_items_per_section,
                total_max_items=chat_settings.total_max_items,
            )
            qa = QaResult(
                passed=True,
                severity="none",
                issues=[],
                warnings=["No processed articles matched current chat settings."],
                recommendation="send",
            )
        else:
            pipeline = self.pipeline_factory(chat_settings.dry_run)
            articles = pipeline.rank_articles_for_digest(
                articles,
                digest_date=effective_date,
                total_max_items=chat_settings.total_max_items,
            )
            digest = pipeline.build_digest(
                articles,
                digest_date=effective_date,
                max_items_per_section=chat_settings.max_items_per_section,
                total_max_items=chat_settings.total_max_items,
            )
            qa = pipeline.quality_check(digest, articles)

        self.storage.save_digest(digest)
        self.storage.log_event(
            "telegram.digest",
            {
                "chat_id": chat_settings.chat_id,
                "digest_date": digest.digest_date,
                "articles": len(articles),
                "qa": qa.model_dump(mode="json"),
                "dry_run": chat_settings.dry_run,
            },
        )
        return digest, qa

    def _send_digest(self, chat_settings: TelegramChatSettings, digest: Digest) -> None:
        TelegramDelivery(self.settings).send(
            digest,
            chat_id=chat_settings.chat_id,
            disable_web_page_preview=chat_settings.disable_web_page_preview,
        )

    def _load_processed_for_chat(
        self,
        chat_settings: TelegramChatSettings,
        digest_date: str,
    ) -> list[ProcessedArticle]:
        return self.storage.load_processed_articles_for_digest(
            digest_date,
            limit=chat_settings.digest_limit,
            timezone_name=chat_settings.timezone,
            source_ids=chat_settings.source_ids,
            min_priority=chat_settings.min_priority,
        )

    def _filter_raw_articles(
        self,
        chat_settings: TelegramChatSettings,
        articles: list[RawArticle],
    ) -> list[RawArticle]:
        if not chat_settings.source_ids:
            return articles
        selected = set(chat_settings.source_ids)
        return [article for article in articles if article.source_id in selected]

    def _selected_enabled_sources(self, chat_settings: TelegramChatSettings) -> list[SourceConfig]:
        sources = load_sources(self.settings.sources_file)
        if not chat_settings.source_ids:
            return sources
        selected = set(chat_settings.source_ids)
        return [source for source in sources if source.id in selected]

    def _all_sources(self) -> list[SourceConfig]:
        return load_sources(self.settings.sources_file, include_disabled=True)

    def _default_digest_date_for_chat(
        self,
        chat_settings: TelegramChatSettings,
        now: datetime | None = None,
    ) -> str:
        now = now or self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return digest_date_or_previous_day(None, chat_settings.timezone, now)

    def _is_delivery_weekday(
        self,
        chat_settings: TelegramChatSettings,
        now: datetime,
    ) -> bool:
        local_now = now.astimezone(zoneinfo_or_utc(chat_settings.timezone))
        return local_now.weekday() in chat_settings.digest_weekdays

    def _send_plain(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        self.api.send_message(
            chat_id,
            text,
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_message_id,
        )

    def _is_authorized(self, chat_id: str, user_id: int, chat_type: str) -> bool:
        if chat_type == "private":
            return True
        if user_id <= 0:
            return False
        if self.admin_checker:
            return self.admin_checker(chat_id, user_id)
        member = self.api.get_chat_member(chat_id, user_id)
        return member.get("status") in {"creator", "administrator"}

    def _default_pipeline_factory(self, dry_run: bool) -> GeminiSkillPipeline:
        llm = (
            DryRunLlmClient()
            if dry_run
            else GeminiClient(
                api_key=self.settings.gemini_api_key,
                model=self.settings.gemini_model,
            )
        )
        return GeminiSkillPipeline(
            llm=llm,
            skill_loader=SkillLoader(self.settings.skills_root),
        )


def parse_command(text: str) -> tuple[str, str] | None:
    if not text.startswith("/"):
        return None
    first, _, rest = text.partition(" ")
    command = first[1:].split("@", 1)[0].strip().lower().replace("-", "_")
    return command, rest.strip()


def _telegram_error_description(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.reason_phrase or "Telegram API request failed"
    if isinstance(data, dict):
        description = data.get("description")
        if description:
            return str(description)
    return response.reason_phrase or "Telegram API request failed"


def format_chat_settings(settings: TelegramChatSettings) -> str:
    source_selection = (
        "all enabled sources" if not settings.source_ids else ", ".join(settings.source_ids)
    )
    previews = "on" if not settings.disable_web_page_preview else "off"
    delivery = "on" if settings.enabled else "off"
    return "\n".join(
        [
            f"Chat: {settings.chat_title or settings.chat_id}",
            f"delivery: {delivery}",
            f"timezone: {settings.timezone}",
            f"digest_time: {settings.digest_time}",
            f"weekdays: {format_weekdays(settings.digest_weekdays)}",
            f"digest_limit: {settings.digest_limit}",
            f"section_limit: {settings.max_items_per_section}",
            f"total_limit: {settings.total_max_items}",
            f"min_priority: {settings.min_priority}",
            f"dry_run: {_bool_text(settings.dry_run)}",
            f"previews: {previews}",
            f"auto_collect: {_bool_text(settings.auto_collect)}",
            f"auto_process: {_bool_text(settings.auto_process)}",
            f"sources: {source_selection}",
            f"last_sent: {settings.last_digest_sent_date or '-'}",
        ]
    )


def format_schedule(settings: TelegramChatSettings) -> str:
    return "\n".join(
        [
            f"timezone: {settings.timezone}",
            f"digest_time: {settings.digest_time}",
            f"weekdays: {format_weekdays(settings.digest_weekdays)}",
            f"delivery: {'on' if settings.enabled else 'off'}",
        ]
    )


def format_weekdays(days: list[int]) -> str:
    normalized = sorted(set(days))
    if normalized == list(range(7)):
        return "daily"
    if normalized == [0, 1, 2, 3, 4]:
        return "weekdays"
    if normalized == [5, 6]:
        return "weekends"
    return ",".join(WEEKDAY_NAMES[day] for day in normalized)


def parse_weekdays(value: str) -> list[int]:
    normalized = value.strip().lower().replace(" ", "")
    aliases = {
        "daily": list(range(7)),
        "everyday": list(range(7)),
        "all": list(range(7)),
        "все": list(range(7)),
        "каждыйдень": list(range(7)),
        "weekdays": [0, 1, 2, 3, 4],
        "workdays": [0, 1, 2, 3, 4],
        "будни": [0, 1, 2, 3, 4],
        "рабочиедни": [0, 1, 2, 3, 4],
        "weekends": [5, 6],
        "выходные": [5, 6],
    }
    if normalized in aliases:
        return aliases[normalized]

    token_map = {
        "0": 0,
        "1": 0,
        "mon": 0,
        "monday": 0,
        "пн": 0,
        "понедельник": 0,
        "2": 1,
        "tue": 1,
        "tuesday": 1,
        "вт": 1,
        "вторник": 1,
        "3": 2,
        "wed": 2,
        "wednesday": 2,
        "ср": 2,
        "среда": 2,
        "4": 3,
        "thu": 3,
        "thursday": 3,
        "чт": 3,
        "четверг": 3,
        "5": 4,
        "fri": 4,
        "friday": 4,
        "пт": 4,
        "пятница": 4,
        "6": 5,
        "sat": 5,
        "saturday": 5,
        "сб": 5,
        "суббота": 5,
        "7": 6,
        "sun": 6,
        "sunday": 6,
        "вс": 6,
        "воскресенье": 6,
    }
    tokens = [token for token in re.split(r"[,;]+", normalized) if token]
    if not tokens:
        raise ValueError("weekdays must not be empty")
    days = []
    for token in tokens:
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            if start_raw not in token_map or end_raw not in token_map:
                raise ValueError(f"Unknown weekday range: {token}")
            start = token_map[start_raw]
            end = token_map[end_raw]
            if start <= end:
                days.extend(range(start, end + 1))
            else:
                days.extend(list(range(start, 7)) + list(range(0, end + 1)))
            continue
        if token not in token_map:
            raise ValueError(f"Unknown weekday: {token}")
        days.append(token_map[token])
    return sorted(set(days))


def _split_key_value(args: str) -> tuple[str, str]:
    key, separator, value = args.strip().partition(" ")
    if not separator or not key or not value.strip():
        raise ValueError("Use: /crypto_set <key> <value>")
    return key, value.strip()


def _normalize_key(value: str) -> str:
    aliases = {
        "time": "digest_time",
        "schedule": "digest_time",
        "tz": "timezone",
        "limit": "digest_limit",
        "max_items": "total_limit",
        "total_max_items": "total_limit",
        "max_items_per_section": "section_limit",
        "enabled": "delivery",
        "send": "delivery",
        "preview": "previews",
        "source_ids": "sources",
        "days": "weekdays",
        "digest_days": "weekdays",
        "schedule_days": "weekdays",
        "дни": "weekdays",
    }
    normalized = value.strip().lower().replace("-", "_")
    return aliases.get(normalized, normalized)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError("Boolean value must be one of: on/off, true/false, yes/no")


def _parse_int(value: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Expected integer from {minimum} to {maximum}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"Expected integer from {minimum} to {maximum}")
    return parsed


def _normalize_time(value: str) -> str:
    return TelegramChatSettings(chat_id="validation", digest_time=value).digest_time


def _validate_chat_settings(settings: TelegramChatSettings) -> None:
    try:
        TelegramChatSettings.model_validate(settings.model_dump())
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _chat_title(chat: dict[str, Any]) -> str | None:
    return (
        chat.get("title")
        or chat.get("username")
        or " ".join(str(chat.get(key) or "") for key in ("first_name", "last_name")).strip()
        or None
    )


def _bool_text(value: bool) -> str:
    return "on" if value else "off"
