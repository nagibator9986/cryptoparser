from crypto_monitor.config import Settings
from crypto_monitor.health import build_health_payload


def test_health_payload_is_ready_when_runtime_dependencies_exist(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    sources_file = tmp_path / "sources.yml"
    sources_file.write_text("sources: []\n", encoding="utf-8")

    settings = Settings(
        CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
        CRYPTO_MONITOR_SKILLS_ROOT=skills_root,
        CRYPTO_MONITOR_SOURCES_FILE=sources_file,
        TELEGRAM_BOT_TOKEN="super-secret-telegram-value",
        GEMINI_API_KEY="super-secret-gemini-value",
    )

    status, payload = build_health_payload(settings)

    assert status == 200
    assert payload["ok"] is True
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["telegram_bot_token"]["configured"] is True
    assert payload["checks"]["gemini_api_key"]["configured"] is True
    assert "super-secret-telegram-value" not in str(payload)
    assert "super-secret-gemini-value" not in str(payload)


def test_health_payload_fails_without_telegram_token(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    sources_file = tmp_path / "sources.yml"
    sources_file.write_text("sources: []\n", encoding="utf-8")

    settings = Settings(
        CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
        CRYPTO_MONITOR_SKILLS_ROOT=skills_root,
        CRYPTO_MONITOR_SOURCES_FILE=sources_file,
    )

    status, payload = build_health_payload(settings)

    assert status == 503
    assert payload["ok"] is False
    assert payload["checks"]["telegram_bot_token"]["configured"] is False


def test_health_payload_fails_without_gemini_key(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    sources_file = tmp_path / "sources.yml"
    sources_file.write_text("sources: []\n", encoding="utf-8")

    settings = Settings(
        CRYPTO_MONITOR_DB_PATH=tmp_path / "db.sqlite3",
        CRYPTO_MONITOR_SKILLS_ROOT=skills_root,
        CRYPTO_MONITOR_SOURCES_FILE=sources_file,
        TELEGRAM_BOT_TOKEN="token",
    )

    status, payload = build_health_payload(settings)

    assert status == 503
    assert payload["ok"] is False
    assert payload["checks"]["gemini_api_key"]["configured"] is False
