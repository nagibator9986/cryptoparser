from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from crypto_monitor.config import Settings
from crypto_monitor.storage import SqliteStorage
from crypto_monitor.telegram_bot import (
    TelegramBotApi,
    TelegramCommandBot,
    TelegramConflictError,
    parse_command,
    parse_weekdays,
)


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
    ) -> None:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_message_id,
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
            {"timeout": 30, "allowed_updates": ["message", "edited_message"]},
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
