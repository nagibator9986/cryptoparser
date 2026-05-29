from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from pydantic import ValidationError

from crypto_monitor.digest_renderer import render_digest_locally
from crypto_monitor.gemini import LlmClient
from crypto_monitor.json_utils import JsonExtractionError
from crypto_monitor.models import Digest, ProcessedArticle, QaResult, RawArticle
from crypto_monitor.normalization import normalize_raw_article
from crypto_monitor.security import sanitize_untrusted_text
from crypto_monitor.skills import SkillLoader, build_user_payload

logger = logging.getLogger(__name__)
PRIORITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
RANKING_PRIORITY = {"low", "medium", "high", "critical"}

RANKING_TASK = (
    "Rank this complete candidate set for the morning digest of a "
    "Kazakhstan-based bank. Compare articles against each other, not in "
    "isolation. Decisions must be reproducible from the payload alone — "
    "no external knowledge required.\n"
    "\n"
    "Scoring rubric (0-100):\n"
    "  90-100 critical : new KZ law, AFSA/AIFC/НБ РК licence or revocation, "
    "                   CBDC launch milestone, major bank crypto product, "
    "                   sanctions, $100M+ security incident.\n"
    "  70-89  high     : official regulator stance, CIS regulator action, "
    "                   significant market infra change, established "
    "                   exchange listing/licensing affecting KZ users.\n"
    "  50-69  medium   : expert commentary with named source, mid-tier "
    "                   product launch, regulatory clarification, notable "
    "                   tokenisation or DeFi case.\n"
    "  20-49  low      : minor token updates, generic market commentary, "
    "                   stale news repackaged.\n"
    "   0-19  drop     : price predictions, promotional content, "
    "                   speculative rumours, near-duplicate of higher "
    "                   ranked items.\n"
    "\n"
    "Hard rules:\n"
    "  - geo_priority 1 articles get +1 step over an equivalent global "
    "    item, but never invent KZ relevance.\n"
    "  - prefer the original publisher over aggregators when scores tie.\n"
    "  - ids in ranked_articles MUST be from the supplied set; never "
    "    invent or transliterate identifiers.\n"
    "  - put low-signal duplicates in dropped_ids with a one-line reason.\n"
    "  - keep ranking_reason under 25 words; cite the rubric tier.\n"
    "\n"
    "Return JSON: {ranked_articles:[{id, priority, score, ranking_reason}],"
    " dropped_ids:[{id, reason}]}."
)


_TIER1_HINTS = (
    "afsa",
    "aifc",
    "nationalbank",
    "gov.kz",
    "ardfm",
    "mdai",
)
_TIER2_HINTS = (
    "kapital",
    "kursiv",
    "forbes.kz",
    "forklog",
    "cbr.ru",
    "cbu.uz",
    "nbkr",
)
_TIER3_HINTS = (
    "coindesk",
    "cointelegraph",
    "theblock",
    "sec.gov",
    "mas.gov.sg",
    "fca.org.uk",
    "bis.org",
    "fatf-gafi",
)


def _source_authority(source_id: str, source_url: str) -> str:
    haystack = f"{source_id} {source_url}".lower()
    if any(hint in haystack for hint in _TIER1_HINTS):
        return "tier1_regulator"
    if any(hint in haystack for hint in _TIER2_HINTS):
        return "tier2_national"
    if any(hint in haystack for hint in _TIER3_HINTS):
        return "tier3_international"
    return "tier4_other"


class GeminiSkillPipeline:
    """Orchestrates the daily AI processing pipeline through Gemini-backed skills."""

    def __init__(self, llm: LlmClient, skill_loader: SkillLoader) -> None:
        self.llm = llm
        self.skill_loader = skill_loader

    def process_articles(self, articles: Iterable[RawArticle]) -> list[ProcessedArticle]:
        processed: list[ProcessedArticle] = []
        for article in articles:
            try:
                item = self.process_one(article)
            except Exception:
                logger.exception("article_processing_failed article_id=%s", article.id)
                continue
            if item.geo_priority != 0 and item.topics:
                processed.append(item)
            else:
                logger.info("article_filtered article_id=%s reason=no_topics", article.id)
        return processed

    def process_one(self, article: RawArticle) -> ProcessedArticle:
        article = normalize_raw_article(article)
        safe_title, title_warnings = sanitize_untrusted_text(article.title, max_chars=500)
        safe_body, body_warnings = sanitize_untrusted_text(article.body)
        result = ProcessedArticle(
            **{
                **article.model_dump(),
                "title": safe_title,
                "body": safe_body,
            },
            original_title=article.title,
            original_body=article.body,
        )
        result.warnings.extend(title_warnings)
        result.warnings.extend(body_warnings)

        if article.language and article.language.lower() != "ru":
            translated = self.call_skill(
                "crypto-news-translator",
                "Translate this article to Russian for the crypto-monitoring digest.",
                result.model_dump(mode="json"),
            )
            result.title = translated.get("title_ru") or result.title
            result.body = translated.get("body_ru") or result.body
            result.title_ru = translated.get("title_ru")
            result.language = "ru"

        classified = self.call_skill(
            "crypto-news-classifier",
            "Classify this news for the digital-assets monitoring digest.",
            result.model_dump(mode="json"),
        )
        result.topics = list(classified.get("topics") or [])
        result.country = classified.get("country")
        result.geo_priority = classified.get("geo_priority")
        result.confidence = classified.get("confidence")

        if not result.topics or result.geo_priority == 0:
            return result

        summarized = self.call_skill(
            "crypto-news-summarizer",
            "Summarize this article in Russian, 60-120 words, copyright-safe.",
            result.model_dump(mode="json"),
        )
        result.title_ru = summarized.get("title_ru") or result.title_ru or result.title
        result.summary = summarized.get("summary")
        result.key_entities = list(summarized.get("key_entities") or [])
        result.warnings.extend(str(item) for item in summarized.get("warnings") or [])

        prioritized = self.call_skill(
            "crypto-news-prioritizer",
            "Prioritize this news for the daily corporate digest.",
            result.model_dump(mode="json"),
        )
        result.priority = prioritized.get("priority")
        result.score = prioritized.get("score")
        return result

    def deduplicate(self, articles: list[ProcessedArticle]) -> list[ProcessedArticle]:
        if not articles:
            return []
        exact = deduplicate_exact(articles)
        try:
            response = self.call_skill(
                "crypto-news-deduplicator",
                "Cluster these articles by event and choose canonical publications.",
                {"articles": [article.model_dump(mode="json") for article in exact]},
            )
        except Exception as exc:
            logger.warning("deduplicator_failed_using_exact_fallback error=%s", exc)
            return exact
        canonical_ids = {cluster.get("canonical_id") for cluster in response.get("clusters", [])}
        canonical_ids.update(response.get("singletons") or [])
        canonical_ids.discard(None)
        if not canonical_ids:
            return exact
        return [article for article in exact if article.id in canonical_ids]

    def build_digest(
        self,
        articles: list[ProcessedArticle],
        digest_date: str | None = None,
        max_items_per_section: int = 5,
        total_max_items: int = 25,
    ) -> Digest:
        payload = {
            "digest_date": digest_date or date.today().isoformat(),
            "articles": [article.model_dump(mode="json") for article in articles],
            "max_items_per_section": max_items_per_section,
            "total_max_items": total_max_items,
        }
        try:
            response = self.call_skill(
                "crypto-digest-builder",
                "Build the final daily digest in HTML, plain text, and Telegram MarkdownV2.",
                payload,
                include_assets=True,
            )
            response.setdefault("digest_date", payload["digest_date"])
            response.setdefault("stats", {"total_articles": len(articles)})
            return Digest.model_validate(response)
        except (JsonExtractionError, ValidationError, ValueError) as exc:
            logger.warning("digest_builder_failed_using_local_renderer error=%s", exc)
            return render_digest_locally(
                articles,
                digest_date=payload["digest_date"],
                max_items_per_section=max_items_per_section,
                total_max_items=total_max_items,
            )

    def rank_articles_for_digest(
        self,
        articles: list[ProcessedArticle],
        *,
        digest_date: str | None = None,
        total_max_items: int = 25,
    ) -> list[ProcessedArticle]:
        if not articles:
            return []
        candidate_window = max(total_max_items * 2, total_max_items)
        candidates = sorted(articles, key=article_sort_key)[:candidate_window]
        try:
            response = self.call_skill(
                "crypto-news-prioritizer",
                RANKING_TASK,
                {
                    "digest_date": digest_date,
                    "ranking_policy": {
                        "audience": (
                            "bank / financial organization in Kazakhstan; "
                            "compliance, treasury, retail product teams"
                        ),
                        "geo_priority": "1=KZ > 2=CIS+CA > 3=global",
                        "max_output_items": total_max_items,
                        "ranking_axes": [
                            "geo impact (Kazakhstan first)",
                            "regulatory/business risk for a bank",
                            "source authority (regulator > national > crypto media)",
                            "novelty (favour first reports over rewrites)",
                            "factual specificity (numbers, names, dates)",
                        ],
                    },
                    "articles": [
                        {
                            "id": article.id,
                            "source_id": article.source_id,
                            "source_name": article.source_name,
                            "source_url": article.source_url,
                            "source_authority": _source_authority(
                                article.source_id, article.source_url
                            ),
                            "author": article.author,
                            "published_at": article.published_at.isoformat()
                            if article.published_at
                            else None,
                            "title": article.title_ru or article.title,
                            "summary": article.summary,
                            "topics": article.topics,
                            "country": article.country,
                            "geo_priority": article.geo_priority,
                            "priority": article.priority,
                            "score": article.score,
                            "key_entities": article.key_entities,
                            "has_image": bool(article.image_url),
                            "warnings": article.warnings,
                        }
                        for article in candidates
                    ],
                },
            )
        except Exception as exc:
            logger.warning("ranking_failed_using_local_sort error=%s", exc)
            return sorted(candidates, key=article_sort_key)[:total_max_items]
        return apply_ranking_response(candidates, response, total_max_items=total_max_items)

    def quality_check(self, digest: Digest, articles: list[ProcessedArticle]) -> QaResult:
        response = self.call_skill(
            "crypto-digest-quality-check",
            "QA this digest before delivery.",
            {
                **digest.model_dump(mode="json"),
                "articles": [article.model_dump(mode="json") for article in articles],
            },
        )
        return QaResult.model_validate(response)

    def run(
        self,
        articles: Iterable[RawArticle],
        digest_date: str | None = None,
    ) -> tuple[list[ProcessedArticle], Digest, QaResult]:
        processed = self.process_articles(articles)
        canonical = self.deduplicate(processed)
        ranked = self.rank_articles_for_digest(
            canonical,
            digest_date=digest_date,
            total_max_items=25,
        )
        digest = self.build_digest(ranked, digest_date=digest_date)
        qa = self.quality_check(digest, ranked)
        return ranked, digest, qa

    def validate_source(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self.call_skill(
            "crypto-source-validator",
            "Evaluate this source for the crypto-monitoring catalog.",
            {"candidate": candidate},
        )

    def call_skill(
        self,
        skill_name: str,
        task: str,
        payload: dict[str, Any],
        include_assets: bool = False,
    ) -> dict[str, Any]:
        skill = self.skill_loader.load(
            skill_name,
            include_references=True,
            include_assets=include_assets,
        )
        return self.llm.generate_json(
            system_prompt=skill.system_prompt,
            user_prompt=build_user_payload(task, payload),
        )


def deduplicate_exact(articles: list[ProcessedArticle]) -> list[ProcessedArticle]:
    canonical_by_key: dict[str, ProcessedArticle] = {}
    ordered_keys: list[str] = []
    for article in articles:
        keys = [_normalized_url_key(article.source_url), _title_key(article)]
        key = next((item for item in keys if item), article.id)
        existing = canonical_by_key.get(key)
        if existing is None:
            canonical_by_key[key] = article
            ordered_keys.append(key)
            continue
        canonical_by_key[key] = _choose_canonical(existing, article)
    return [canonical_by_key[key] for key in ordered_keys]


def apply_ranking_response(
    articles: list[ProcessedArticle],
    response: dict[str, Any],
    *,
    total_max_items: int,
) -> list[ProcessedArticle]:
    by_id = {article.id: article for article in articles}
    ranked: list[ProcessedArticle] = []
    seen: set[str] = set()
    for index, item in enumerate(response.get("ranked_articles") or []):
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("id") or "")
        article = by_id.get(article_id)
        if not article or article.id in seen:
            continue
        priority = str(item.get("priority") or article.priority or "medium").lower()
        score = _bounded_score(item.get("score"), fallback=article.score or 50)
        article.priority = priority if priority in RANKING_PRIORITY else article.priority
        article.score = max(score, total_max_items - index)
        reason = item.get("ranking_reason") or item.get("reasoning")
        if reason:
            article.ranking_reason = str(reason)
        ranked.append(article)
        seen.add(article.id)

    dropped_ids: set[str] = set()
    for item in response.get("dropped_ids") or []:
        if isinstance(item, dict):
            article_id = item.get("id")
            if article_id:
                dropped_ids.add(str(article_id))
        elif item:
            dropped_ids.add(str(item))
    for article in sorted(articles, key=article_sort_key):
        if article.id in seen or article.id in dropped_ids:
            continue
        ranked.append(article)
        seen.add(article.id)
    return ranked[:total_max_items]


def _normalized_url_key(url: str) -> str:
    normalized = url.strip().lower()
    normalized = normalized.split("#", 1)[0]
    normalized = re.sub(r"[?&](utm_[^=&]+|fbclid|gclid)=[^&]+", "", normalized)
    normalized = normalized.rstrip("?&/")
    return f"url:{normalized}" if normalized else ""


def article_sort_key(article: ProcessedArticle) -> tuple[int, int, int, float]:
    geo_priority = article.geo_priority if article.geo_priority in {1, 2, 3} else 4
    published = article.published_at.timestamp() if article.published_at else 0.0
    return (
        geo_priority,
        -PRIORITY_RANK.get(article.priority or "medium", 2),
        -(article.score or 0),
        -published,
    )


def _bounded_score(value: object, fallback: int) -> int:
    try:
        score = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(0, min(100, score))


def _title_key(article: ProcessedArticle) -> str:
    title = re.sub(r"\W+", " ", (article.title_ru or article.title).lower()).strip()
    if len(title) < 16:
        return ""
    return f"title:{article.source_id}:{title}"


def _choose_canonical(left: ProcessedArticle, right: ProcessedArticle) -> ProcessedArticle:
    left_rank = _source_rank(left)
    right_rank = _source_rank(right)
    if left_rank != right_rank:
        return left if left_rank < right_rank else right
    left_published = left.published_at.timestamp() if left.published_at else float("inf")
    right_published = right.published_at.timestamp() if right.published_at else float("inf")
    if left_published != right_published:
        return left if left_published < right_published else right
    return left if (left.score or 0) >= (right.score or 0) else right


def _source_rank(article: ProcessedArticle) -> int:
    source = f"{article.source_id} {article.source_name} {article.source_url}".lower()
    if any(marker in source for marker in ("gov", "sec", "cftc", "afsa", "aifc", "nationalbank")):
        return 1
    if any(marker in source for marker in ("forbes", "kapital", "kursiv", "forklog")):
        return 2
    if any(marker in source for marker in ("coindesk", "cointelegraph", "theblock")):
        return 3
    if "telegram" in source or "t.me" in source:
        return 4
    return 5
