from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment and optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="local", alias="CRYPTO_MONITOR_ENV")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_timeout_seconds: float = Field(default=60.0, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_max_retries: int = Field(default=3, alias="GEMINI_MAX_RETRIES")
    process_concurrency: int = Field(default=5, alias="CRYPTO_MONITOR_PROCESS_CONCURRENCY")
    collect_concurrency: int = Field(default=8, alias="CRYPTO_MONITOR_COLLECT_CONCURRENCY")
    digest_lookback_days: int = Field(default=1, alias="CRYPTO_MONITOR_DIGEST_LOOKBACK_DAYS")
    db_path: Path = Field(
        default=Path("./data/crypto_monitor.sqlite3"),
        alias="CRYPTO_MONITOR_DB_PATH",
    )
    skills_root: Path = Field(
        default=Path("./crypto-monitor-skills"),
        alias="CRYPTO_MONITOR_SKILLS_ROOT",
    )
    sources_file: Path = Field(
        default=Path("./config/sources.example.yml"),
        alias="CRYPTO_MONITOR_SOURCES_FILE",
    )

    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    kgd_rates_url: str = Field(
        default="https://token.qoldau.kz/ru/references/crypto-currency/list",
        alias="CRYPTO_MONITOR_KGD_RATES_URL",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
