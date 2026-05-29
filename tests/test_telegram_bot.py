from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from crypto_monitor.config import Settings
from crypto_monitor.models import QaResult
from crypto_monitor.storage import SqliteStorage
from crypto_monitor.telegram_bot import (
    TelegramBotApi,
    TelegramCommandBot,
    TelegramConflictError,
    _format_qa_issues,
    _is_hard_qa_block,
    _parse_digest_args,
    _qa_advisory_note,
    parse_command,
    parse_weekdays,
)


def test_is_hard_qa_block_only_on_blocker_severity() -> None:
    blocker = QaResult(
        passed=False, severity="blocker", recommendation="do_not_send"
    )
    major = QaResult(
        passed=False, severity="major", recommendation="do_not_send"
    )
    sent = QaResult(passed=True, severity="none", recommendation="send")
    assert _is_hard_qa_block(blocker) is True
    assert _is_hard_qa_block(major) is False
    assert _is_hard_qa_block(sent) is False


def test_parse_digest_args_handles_date_and_force_in_any_order() -> None:
    assert _parse_digest_args("") == (None, False)
    assert _parse_digest_args("force") == (None, True)
    assert _parse_digest_args("2026-05-29") == ("2026-05-29", False)
    assert _parse_digest_args("2026-05-29 force") == ("2026-05-29", True)
    assert _parse_digest_args("force 2026-05-29") == ("2026-05-29", True)
    assert _parse_digest_args("принудительно") == (None, True)


def test_qa_advisory_note_silent_on_clean_send() -> None:
    clean = QaResult(passed=True, severity="none", recommendation="send")
    assert _qa_advisory_note(clean) == ""


def test_qa_advisory_note_summarises_warnings_with_issues() -> None:
    qa = QaResult(
        passed=False,
        severity="major",
        recommendation="do_not_send",
        issues=[{"category": "tone", "description": "Sensational adjectives detected"}],
    )
    note = _qa_advisory_note(qa)
    assert "severity=major" in note
    assert "tone" in note
    assert "Sensational adjectives" in note


def test_format_qa_issues_truncates_long_descriptions() -> None:
    qa = QaResult(
        passed=False,
        severity="major",
        recommendation="send_with_caution",
        issues=[{"category": "copyright", "description": "x" * 500}],
    )
    block = _format_qa_issues(qa)
    assert block.startswith("Замечания QA:")
    assert "copyright" in block
    assert len(block.splitlines()[1]) <= 240


class RecordingTelegramApi(TelegramBotApi):
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], float | None]] = []

    def request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        request_timeout: float | None = None,
    ) -> Any:
        self.requests.append((method, payload, request_timeout))
        return []


class FakeTelegramApi:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.deleted_webhook = False

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        self.deleted_webhook = True
        return True

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        return []

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        return {"status": "administrator"}

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
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        self.answered_callbacks.append(
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
        )


class FailingDeleteWebhookApi(FakeTelegramApi):
    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        raise RuntimeError("deleteWebhook failed")


def test_parse_command_strips_bot_username() -> None:
    assert parse_command("/crypto_set@CryptoMonitorBot digest_time 09:05") == (
        "crypto_set",
        "digest_time 09:05",
    )


def test_telegram_bot_deletes_webhook_before_long_polling(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
    )

    bot.prepare_long_polling()

    assert api.deleted_webhook is True


def test_telegram_bot_keeps_running_if_delete_webhook_fails(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FailingDeleteWebhookApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
    )

    bot.prepare_long_polling()


def test_telegram_get_updates_uses_timeout_larger_than_long_poll() -> None:
    api = RecordingTelegramApi()

    api.get_updates(timeout=30)

    assert api.requests == [
        (
            "getUpdates",
            {
                "timeout": 30,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            45,
        )
    ]


def test_telegram_api_raises_redacted_conflict_error(monkeypatch) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": (
                    "Conflict: terminated by other getUpdates request; "
                    "make sure that only one bot instance is running"
                ),
            },
            request=httpx.Request(
                "POST",
                "https://api.telegram.org/botsecret-token/getUpdates",
            ),
        )

    monkeypatch.setattr("crypto_monitor.telegram_bot.httpx.post", fake_post)
    api = TelegramBotApi("secret-token")

    try:
        api.get_updates()
    except TelegramConflictError as exc:
        message = str(exc)
    else:
        raise AssertionError("TelegramConflictError was not raised")

    assert "Conflict:" in message
    assert "secret-token" not in message
    assert "api.telegram.org" not in message


def test_telegram_set_command_updates_group_settings(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )

    bot.handle_update(_message("/crypto_set digest_time 9:05"))

    saved = storage.load_telegram_chat_settings("-1001")
    assert saved is not None
    assert saved.digest_time == "09:05"
    assert "Настройка сохранена" in api.sent[-1]["text"]


def test_telegram_set_command_requires_group_admin(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: False,
    )

    bot.handle_update(_message("/crypto_set dry_run on"))

    assert storage.load_telegram_chat_settings("-1001") is None
    assert "только администраторам" in api.sent[-1]["text"]


def test_telegram_sources_command_saves_selected_sources(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )

    bot.handle_update(_message("/crypto_sources coindesk,afsa-aifc"))

    saved = storage.load_telegram_chat_settings("-1001")
    assert saved is not None
    assert saved.source_ids == ["coindesk", "afsa-aifc"]
    assert "Current source selection: coindesk, afsa-aifc" in api.sent[-1]["text"]


def test_telegram_schedule_command_updates_time_and_weekdays(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )

    bot.handle_update(_message("/crypto_schedule 10:15 weekdays"))

    saved = storage.load_telegram_chat_settings("-1001")
    assert saved is not None
    assert saved.digest_time == "10:15"
    assert saved.digest_weekdays == [0, 1, 2, 3, 4]
    assert "Расписание сохранено" in api.sent[-1]["text"]


def test_parse_weekdays_supports_russian_aliases() -> None:
    assert parse_weekdays("пн,ср,пт") == [0, 2, 4]
    assert parse_weekdays("выходные") == [5, 6]
    assert parse_weekdays("пн-пт") == [0, 1, 2, 3, 4]
    assert parse_weekdays("fri-mon") == [0, 4, 5, 6]


def test_telegram_search_command_is_read_only(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: False,
    )

    bot.handle_update(_message("/crypto_search AFSA"))

    assert "Результаты поиска" in api.sent[-1]["text"]
    assert storage.load_telegram_chat_settings("-1001") is None


def test_telegram_default_digest_date_uses_previous_almaty_day(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        now_provider=lambda: datetime(2026, 5, 27, 4, 0, tzinfo=UTC),
    )
    settings = storage.get_or_create_telegram_chat_settings("-1001")

    assert bot._default_digest_date_for_chat(settings) == "2026-05-26"


def test_telegram_menu_command_sends_inline_keyboard(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )

    bot.handle_update(_message("/crypto_menu"))

    assert api.sent, "expected at least one message"
    markup = api.sent[-1]["reply_markup"]
    assert markup is not None
    callback_data = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    assert "cm:menu:settings" in callback_data
    assert "cm:menu:actions" in callback_data


def test_telegram_callback_toggle_flips_delivery(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )
    storage.get_or_create_telegram_chat_settings("-1001", "Crypto Desk")

    bot.handle_update(_callback("cm:toggle:delivery"))

    saved = storage.load_telegram_chat_settings("-1001")
    assert saved is not None
    assert saved.enabled is True
    assert api.answered_callbacks[-1]["text"].startswith("Доставка:")
    assert api.edited, "expected the menu to be redrawn after toggle"


def test_collect_command_returns_ack_immediately_when_run_in_background(tmp_path) -> None:
    import time as _time

    # Test against an empty sources file so the background worker has
    # nothing to fetch and finishes deterministically without hitting
    # the network.
    sources_path = tmp_path / "sources.yml"
    sources_path.write_text("sources: []\n", encoding="utf-8")

    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=Settings(
            TELEGRAM_BOT_TOKEN="token",
            CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
            CRYPTO_MONITOR_SOURCES_FILE=sources_path,
        ),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: True,
    )

    bot.handle_update(_message("/crypto_collect"))

    assert api.sent, "expected an immediate ack message"
    assert api.sent[-1]["text"].startswith("Запускаю: сбор")

    # Polling loop is no longer blocked. The background worker eventually
    # posts the real result; with no sources configured the result is
    # the empty-sources notice.
    for _ in range(50):
        if any("Нет включенных источников" in entry["text"] for entry in api.sent):
            break
        _time.sleep(0.05)
    else:
        raise AssertionError("background result never arrived")


def test_telegram_callback_rejects_non_admin(tmp_path) -> None:
    storage = SqliteStorage(tmp_path / "db.sqlite3")
    api = FakeTelegramApi()
    bot = TelegramCommandBot(
        settings=_settings(tmp_path),
        storage=storage,
        api=cast(TelegramBotApi, api),
        admin_checker=lambda chat_id, user_id: False,
    )
    storage.get_or_create_telegram_chat_settings("-1001", "Crypto Desk")

    bot.handle_update(_callback("cm:toggle:delivery"))

    saved = storage.load_telegram_chat_settings("-1001")
    assert saved is not None
    assert saved.enabled is False
    assert api.answered_callbacks[-1]["show_alert"] is True
    assert "администраторы" in api.answered_callbacks[-1]["text"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="token",
        CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
        CRYPTO_MONITOR_SOURCES_FILE=Path("config/sources.example.yml"),
    )


def _message(text: str) -> dict[str, Any]:
    return {
        "message": {
            "message_id": 10,
            "text": text,
            "chat": {"id": -1001, "type": "group", "title": "Crypto Desk"},
            "from": {"id": 42},
        }
    }


def _callback(data: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb-1",
            "data": data,
            "from": {"id": 42},
            "message": {
                "message_id": 11,
                "chat": {"id": -1001, "type": "group", "title": "Crypto Desk"},
            },
        }
    }
