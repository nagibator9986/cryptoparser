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
    "cm_menu": "crypto_menu",
    "crypto_main": "crypto_menu",
    "crypto_panel": "crypto_menu",
}

READ_ONLY_COMMANDS = {
    "crypto_help",
    "crypto_settings",
    "crypto_search",
    "crypto_menu",
}

CALLBACK_PREFIX = "cm"

HELP_TEXT = (
    "Crypto Monitor — ежедневная сводка новостей о цифровых активах "
    "для банка в Казахстане. Источники собираются автоматически, "
    "обрабатываются Gemini и публикуются в эту группу.\n"
    "\n"
    "── Быстрый старт ──\n"
    "/crypto_start — зарегистрировать группу и открыть меню\n"
    "/crypto_menu — открыть меню с кнопками в любой момент\n"
    "/crypto_help — этот текст\n"
    "\n"
    "── Просмотр (доступно всем) ──\n"
    "/crypto_settings — показать настройки группы\n"
    "/crypto_search <текст> — поиск по архиву публикаций и сводок\n"
    "\n"
    "── Настройка ──\n"
    "/crypto_set <ключ> <значение> — изменить настройку\n"
    "/crypto_unset <ключ> — сброс sources, min_priority, weekdays или last_sent\n"
    "\n"
    "Ключи /crypto_set:\n"
    "  delivery on|off          — плановая отправка по расписанию\n"
    "  timezone Asia/Almaty     — часовой пояс расписания (IANA)\n"
    "  digest_time HH:MM        — время отправки в локальной TZ\n"
    "  weekdays <значение>      — дни отправки\n"
    "  digest_limit 1..100      — сколько raw-статей брать в обработку\n"
    "  section_limit 1..20      — максимум публикаций в одной секции\n"
    "  total_limit 1..100       — итоговый размер сводки\n"
    "  min_priority low|medium|high|critical — фильтр по приоритету\n"
    "  dry_run on|off           — режим без вызовов Gemini API\n"
    "  previews on|off          — превью ссылок в Telegram\n"
    "  auto_collect on|off      — авто-сбор перед плановой сводкой\n"
    "  auto_process on|off      — авто-обработка перед плановой сводкой\n"
    "\n"
    "── Расписание ──\n"
    "/crypto_schedule — показать текущее расписание\n"
    "/crypto_schedule HH:MM — изменить только время отправки\n"
    "/crypto_schedule HH:MM <дни> — время и дни одной командой\n"
    "Значения дней: daily, weekdays, weekends, mon-fri, mon,wed,fri, "
    "пн-пт, пн,ср,пт, будни, выходные.\n"
    "\n"
    "── Источники ──\n"
    "/crypto_sources — каталог и текущий выбор\n"
    "/crypto_sources all — использовать все включённые источники\n"
    "/crypto_sources id1,id2 — оставить только перечисленные id\n"
    "\n"
    "── Пайплайн вручную ──\n"
    "/crypto_collect — собрать raw-статьи из выбранных источников\n"
    "/crypto_process — пропустить raw через Gemini skills\n"
    "/crypto_digest [YYYY-MM-DD] — собрать и отправить сводку\n"
    "/crypto_latest [YYYY-MM-DD] — отправить архивную сводку\n"
    "/crypto_run [YYYY-MM-DD] — collect + process + digest одной командой\n"
    "\n"
    "── Доступы ──\n"
    "Менять настройки и запускать пайплайн могут только администраторы "
    "группы. /crypto_help, /crypto_menu, /crypto_settings и /crypto_search "
    "доступны всем участникам.\n"
    "\n"
    "── Алиасы ──\n"
    "Все команды доступны также с префиксом /cm_ (например /cm_start, "
    "/cm_menu, /cm_digest)."
)


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
            "allowed_updates": ["message", "edited_message", "callback_query"],
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
        reply_markup: dict[str, Any] | None = None,
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
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("editMessageText", payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]
        self.request("answerCallbackQuery", payload)


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
        callback = update.get("callback_query")
        if callback:
            self._handle_callback_query(callback)
            return

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
            self._send_plain(
                chat_id,
                "Команды настройки и запуска доступны только администраторам группы.",
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            response = self._dispatch(
                command,
                args,
                chat_id,
                chat_title,
                reply_to_message_id,
            )
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
        reply_to_message_id: int | None = None,
    ) -> str | None:
        if command == "crypto_help":
            return HELP_TEXT
        if command == "crypto_menu":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            self._send_keyboard(
                chat_id,
                self._main_menu_text(chat_settings),
                self._main_menu_markup(),
                reply_to_message_id=reply_to_message_id,
            )
            return None
        if command == "crypto_start":
            chat_settings = self.storage.get_or_create_telegram_chat_settings(
                chat_id,
                chat_title,
            )
            text = "Группа подключена.\n\n" + self._main_menu_text(chat_settings)
            self._send_keyboard(
                chat_id,
                text,
                self._main_menu_markup(),
                reply_to_message_id=reply_to_message_id,
            )
            return None
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

    # ── Inline-menu UI ──

    def _send_keyboard(
        self,
        chat_id: str,
        text: str,
        markup: dict[str, Any],
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        self.api.send_message(
            chat_id,
            text,
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_message_id,
            reply_markup=markup,
        )

    def _main_menu_text(self, chat_settings: TelegramChatSettings) -> str:
        return (
            "Crypto Monitor — главное меню\n"
            "Выберите раздел кнопками ниже.\n"
            "\n"
            + format_chat_settings(chat_settings)
        )

    def _menu_text(self, chat_settings: TelegramChatSettings, target: str) -> str:
        if target == "settings":
            return "Настройки группы\n\n" + format_chat_settings(chat_settings)
        if target == "schedule":
            return (
                "Расписание плановой отправки.\n"
                "Время и таймзону меняйте через /crypto_set, дни — кнопками ниже.\n"
                "\n"
                + format_schedule(chat_settings)
            )
        if target == "priority":
            return (
                "Минимальный приоритет публикаций в сводке.\n"
                f"Сейчас: {chat_settings.min_priority}.\n"
                "Публикации ниже выбранного уровня в сводку не попадают."
            )
        if target == "sources":
            selected = (
                "все включённые" if not chat_settings.source_ids
                else ", ".join(chat_settings.source_ids)
            )
            return (
                "Источники.\n"
                f"Текущий выбор: {selected}.\n"
                "Кнопки переключают конкретные источники; "
                "«Все включённые» сбрасывает выбор."
            )
        if target == "actions":
            return (
                "Действия пайплайна.\n"
                "Каждая кнопка выполняет ту же команду, что и слэш-команда.\n"
                "Тяжёлые операции (Gemini) могут занять до минуты — "
                "результат придёт отдельным сообщением."
            )
        return self._main_menu_text(chat_settings)

    @staticmethod
    def _main_menu_markup() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Настройки", "callback_data": "cm:menu:settings"},
                    {"text": "Расписание", "callback_data": "cm:menu:schedule"},
                ],
                [
                    {"text": "Источники", "callback_data": "cm:menu:sources"},
                    {"text": "Приоритет", "callback_data": "cm:menu:priority"},
                ],
                [
                    {"text": "Действия", "callback_data": "cm:menu:actions"},
                    {"text": "Помощь", "callback_data": "cm:help"},
                ],
            ]
        }

    @staticmethod
    def _settings_menu_markup(s: TelegramChatSettings) -> dict[str, Any]:
        def toggle(label: str, key: str, current: bool) -> dict[str, str]:
            mark = "ON" if current else "OFF"
            return {"text": f"{label}: {mark}", "callback_data": f"cm:toggle:{key}"}

        return {
            "inline_keyboard": [
                [toggle("Доставка", "delivery", s.enabled)],
                [toggle("Dry-run", "dry_run", s.dry_run)],
                [toggle("Auto-сбор", "auto_collect", s.auto_collect)],
                [toggle("Auto-обработка", "auto_process", s.auto_process)],
                [toggle("Превью ссылок", "previews", not s.disable_web_page_preview)],
                [{"text": "<< В меню", "callback_data": "cm:menu:main"}],
            ]
        }

    @staticmethod
    def _schedule_menu_markup(s: TelegramChatSettings) -> dict[str, Any]:
        current = format_weekdays(s.digest_weekdays)

        def days(label: str, preset: str) -> dict[str, str]:
            mark = " *" if current == preset else ""
            return {"text": f"{label}{mark}", "callback_data": f"cm:weekdays:{preset}"}

        return {
            "inline_keyboard": [
                [days("Каждый день", "daily"), days("Будни", "weekdays")],
                [days("Выходные", "weekends")],
                [{"text": "<< В меню", "callback_data": "cm:menu:main"}],
            ]
        }

    @staticmethod
    def _priority_menu_markup(s: TelegramChatSettings) -> dict[str, Any]:
        def prio(label: str, value: str) -> dict[str, str]:
            mark = " *" if s.min_priority == value else ""
            return {"text": f"{label}{mark}", "callback_data": f"cm:priority:{value}"}

        return {
            "inline_keyboard": [
                [prio("Low", "low"), prio("Medium", "medium")],
                [prio("High", "high"), prio("Critical", "critical")],
                [{"text": "<< В меню", "callback_data": "cm:menu:main"}],
            ]
        }

    def _sources_menu_markup(self, s: TelegramChatSettings) -> dict[str, Any]:
        sources = self._all_sources()
        selected = set(s.source_ids)
        using_all = not selected
        rows: list[list[dict[str, str]]] = [
            [
                {
                    "text": ("[*] Все включённые" if using_all else "Все включённые"),
                    "callback_data": "cm:src:all",
                }
            ]
        ]
        for source in sources:
            if not source.enabled:
                continue
            chosen = using_all or source.id in selected
            mark = "[*]" if chosen else "[ ]"
            rows.append(
                [
                    {
                        "text": f"{mark} {source.id}",
                        "callback_data": f"cm:src:toggle:{source.id}",
                    }
                ]
            )
        rows.append([{"text": "<< В меню", "callback_data": "cm:menu:main"}])
        return {"inline_keyboard": rows}

    @staticmethod
    def _actions_menu_markup() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Сбор raw", "callback_data": "cm:run:collect"},
                    {"text": "Обработка", "callback_data": "cm:run:process"},
                ],
                [
                    {"text": "Собрать сводку", "callback_data": "cm:run:digest"},
                    {"text": "Архив (последняя)", "callback_data": "cm:run:latest"},
                ],
                [{"text": "<< В меню", "callback_data": "cm:menu:main"}],
            ]
        }

    def _render_menu(
        self,
        chat_id: str,
        message_id: int | None,
        chat_settings: TelegramChatSettings,
        target: str,
    ) -> None:
        markups = {
            "main": self._main_menu_markup(),
            "settings": self._settings_menu_markup(chat_settings),
            "schedule": self._schedule_menu_markup(chat_settings),
            "priority": self._priority_menu_markup(chat_settings),
            "sources": self._sources_menu_markup(chat_settings),
            "actions": self._actions_menu_markup(),
        }
        markup = markups.get(target, markups["main"])
        text = (
            self._main_menu_text(chat_settings)
            if target == "main"
            else self._menu_text(chat_settings, target)
        )
        if message_id is None:
            self._send_keyboard(chat_id, text, markup)
            return
        try:
            self.api.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=markup,
            )
        except TelegramApiError as exc:
            logger.warning(
                "telegram_edit_message_failed chat_id=%s target=%s: %s",
                chat_id,
                target,
                exc,
            )
            self._send_keyboard(chat_id, text, markup)

    # ── Callback queries (button taps) ──

    def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = message.get("message_id")
        chat_title = _chat_title(chat)
        chat_type = str(chat.get("type") or "")
        user = callback.get("from") or {}
        user_id = int(user.get("id") or 0)

        if not chat_id or not data.startswith(f"{CALLBACK_PREFIX}:"):
            if callback_id:
                self._safe_answer_callback(callback_id)
            return

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        args = parts[2:]

        write_actions = {"toggle", "priority", "weekdays", "src", "run"}
        if action in write_actions and not self._is_authorized(
            chat_id,
            user_id,
            chat_type,
        ):
            self._safe_answer_callback(
                callback_id,
                text="Только администраторы группы могут менять настройки.",
                alert=True,
            )
            return

        # Long-running actions: ack immediately, then run in foreground.
        if action == "run":
            self._safe_answer_callback(callback_id, text="Запускаю...")
            try:
                self._run_callback_action(args, chat_id, chat_title)
            except Exception as exc:
                logger.exception(
                    "telegram_callback_run_failed args=%s chat_id=%s",
                    args,
                    chat_id,
                )
                self._send_plain(
                    chat_id,
                    f"Ошибка: {type(exc).__name__}: {exc}",
                )
            return

        try:
            notice = self._dispatch_callback(
                action,
                args,
                chat_id,
                chat_title,
                message_id,
            )
        except Exception as exc:
            logger.exception(
                "telegram_callback_failed data=%s chat_id=%s",
                data,
                chat_id,
            )
            notice = f"Ошибка: {type(exc).__name__}: {exc}"

        self._safe_answer_callback(callback_id, text=notice or "")

    def _dispatch_callback(
        self,
        action: str,
        args: list[str],
        chat_id: str,
        chat_title: str | None,
        message_id: int | None,
    ) -> str:
        if action == "help":
            self._send_plain(chat_id, HELP_TEXT)
            return "Открыл помощь"

        chat_settings = self.storage.get_or_create_telegram_chat_settings(
            chat_id,
            chat_title,
        )

        if action == "menu":
            target = args[0] if args else "main"
            self._render_menu(chat_id, message_id, chat_settings, target)
            return ""

        if action == "toggle":
            key = args[0] if args else ""
            if key == "delivery":
                chat_settings.enabled = not chat_settings.enabled
                notice = f"Доставка: {'ON' if chat_settings.enabled else 'OFF'}"
            elif key == "dry_run":
                chat_settings.dry_run = not chat_settings.dry_run
                notice = f"Dry-run: {'ON' if chat_settings.dry_run else 'OFF'}"
            elif key == "auto_collect":
                chat_settings.auto_collect = not chat_settings.auto_collect
                notice = f"Auto-сбор: {'ON' if chat_settings.auto_collect else 'OFF'}"
            elif key == "auto_process":
                chat_settings.auto_process = not chat_settings.auto_process
                notice = (
                    f"Auto-обработка: {'ON' if chat_settings.auto_process else 'OFF'}"
                )
            elif key == "previews":
                chat_settings.disable_web_page_preview = (
                    not chat_settings.disable_web_page_preview
                )
                state = "OFF" if chat_settings.disable_web_page_preview else "ON"
                notice = f"Превью ссылок: {state}"
            else:
                return f"Неизвестная настройка: {key}"
            self.storage.save_telegram_chat_settings(chat_settings)
            self._render_menu(chat_id, message_id, chat_settings, "settings")
            return notice

        if action == "priority":
            value = args[0] if args else "low"
            if value not in PRIORITY_RANK:
                return f"Недопустимый приоритет: {value}"
            chat_settings.min_priority = cast(PriorityName, value)
            self.storage.save_telegram_chat_settings(chat_settings)
            self._render_menu(chat_id, message_id, chat_settings, "priority")
            return f"min_priority: {value}"

        if action == "weekdays":
            preset = args[0] if args else "daily"
            presets = {
                "daily": list(range(7)),
                "weekdays": [0, 1, 2, 3, 4],
                "weekends": [5, 6],
            }
            if preset not in presets:
                return f"Неизвестный набор: {preset}"
            chat_settings.digest_weekdays = presets[preset]
            self.storage.save_telegram_chat_settings(chat_settings)
            self._render_menu(chat_id, message_id, chat_settings, "schedule")
            return f"Дни: {preset}"

        if action == "src":
            sub = args[0] if args else ""
            if sub == "all":
                chat_settings.source_ids = []
                self.storage.save_telegram_chat_settings(chat_settings)
                self._render_menu(chat_id, message_id, chat_settings, "sources")
                return "Источники: все включённые"
            if sub == "toggle" and len(args) > 1:
                source_id = args[1]
                enabled_ids = [src.id for src in self._all_sources() if src.enabled]
                current = set(chat_settings.source_ids) or set(enabled_ids)
                if source_id in current:
                    current.discard(source_id)
                else:
                    current.add(source_id)
                chat_settings.source_ids = sorted(current & set(enabled_ids))
                self.storage.save_telegram_chat_settings(chat_settings)
                self._render_menu(chat_id, message_id, chat_settings, "sources")
                return f"Источник {source_id} переключён"
            return "Неизвестное действие источников"

        return ""

    def _run_callback_action(
        self,
        args: list[str],
        chat_id: str,
        chat_title: str | None,
    ) -> None:
        target = args[0] if args else ""
        chat_settings = self.storage.get_or_create_telegram_chat_settings(
            chat_id,
            chat_title,
        )
        if target == "collect":
            self._send_plain(chat_id, "Запускаю сбор...")
            self._send_plain(chat_id, self._collect_for_chat(chat_settings))
            return
        if target == "process":
            self._send_plain(chat_id, "Запускаю обработку...")
            self._send_plain(chat_id, self._process_for_chat(chat_settings))
            return
        if target == "digest":
            self._send_plain(chat_id, "Собираю сводку...")
            self._send_plain(chat_id, self._digest_command(chat_settings, ""))
            return
        if target == "latest":
            self._send_plain(chat_id, self._latest_command(chat_settings, ""))
            return
        self._send_plain(chat_id, f"Неизвестное действие: {target}")

    def _safe_answer_callback(
        self,
        callback_id: str,
        *,
        text: str = "",
        alert: bool = False,
    ) -> None:
        if not callback_id or not hasattr(self.api, "answer_callback_query"):
            return
        try:
            self.api.answer_callback_query(
                callback_id,
                text=text,
                show_alert=alert,
            )
        except Exception as exc:
            logger.warning(
                "telegram_answer_callback_failed: %s: %s",
                type(exc).__name__,
                exc,
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
