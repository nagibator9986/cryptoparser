from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

WEEKDAY_NAMES = {
    0: "mon",
    1: "tue",
    2: "wed",
    3: "thu",
    4: "fri",
    5: "sat",
    6: "sun",
}


class SourceType(StrEnum):
    RSS = "rss"
    HTML = "html"
    TELEGRAM = "telegram"
    X_ACCOUNT = "x_account"
    JSON_API = "json_api"


class SourceConfig(BaseModel):
    id: str
    name: str
    url: HttpUrl
    type: SourceType
    enabled: bool = True
    language_hint: str | None = None
    country_hint: str | None = None
    poll_interval_minutes: int = 30
    priority_hint: int | None = Field(default=None, ge=1, le=3)


class RawArticle(BaseModel):
    id: str
    source_id: str
    source_name: str
    source_url: str
    title: str
    body: str
    published_at: datetime | None = None
    language: str | None = None
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    author: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProcessedArticle(RawArticle):
    original_title: str | None = None
    original_body: str | None = None
    title_ru: str | None = None
    summary: str | None = None
    topics: list[str] = Field(default_factory=list)
    country: str | None = None
    geo_priority: int | None = None
    confidence: float | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    key_entities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ranking_reason: str | None = None


class TelegramArticleBlock(BaseModel):
    section: str
    title: str
    summary: str
    source_name: str
    source_url: str
    published_at_text: str
    priority: str
    image_url: str | None = None


class Digest(BaseModel):
    digest_date: str
    html: str
    plain_text: str
    telegram_segments: list[str]
    telegram_articles: list[TelegramArticleBlock] = Field(default_factory=list)
    header_text: str | None = None
    footer_text: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


class QaResult(BaseModel):
    passed: bool
    severity: str
    issues: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any] | str] = Field(default_factory=list)
    recommendation: str


class PipelineResult(BaseModel):
    articles: list[ProcessedArticle]
    digest: Digest | None = None
    qa: QaResult | None = None


class TelegramChatSettings(BaseModel):
    chat_id: str
    chat_title: str | None = None
    enabled: bool = False
    timezone: str = "Asia/Almaty"
    digest_time: str = "09:00"
    digest_weekdays: list[int] = Field(default_factory=lambda: list(range(7)))
    digest_limit: int = Field(default=25, ge=1, le=100)
    max_items_per_section: int = Field(default=5, ge=1, le=20)
    total_max_items: int = Field(default=25, ge=1, le=100)
    min_priority: Literal["low", "medium", "high", "critical"] = "low"
    dry_run: bool = False
    disable_web_page_preview: bool = True
    auto_collect: bool = False
    auto_process: bool = False
    source_ids: list[str] = Field(default_factory=list)
    last_digest_sent_date: str | None = None

    @field_validator("digest_time")
    @classmethod
    def validate_digest_time(cls, value: str) -> str:
        try:
            hour_raw, minute_raw = value.split(":", 1)
            hour = int(hour_raw)
            minute = int(minute_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("digest_time must use HH:MM format") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("digest_time must use HH:MM format")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("digest_weekdays")
    @classmethod
    def validate_digest_weekdays(cls, value: list[int]) -> list[int]:
        unique = sorted(set(value))
        if not unique:
            raise ValueError("digest_weekdays must contain at least one weekday")
        if any(day < 0 or day > 6 for day in unique):
            raise ValueError("digest_weekdays values must be between 0 and 6")
        return unique
