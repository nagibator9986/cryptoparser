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
    "  90-100 critical : KZ legislation on digital assets at ANY stage "
    "                   (is_legislative=true + geo_priority=1); AFSA/AIFC/"
    "                   НБ РК licence, revocation or guidance; CBDC launch "
    "                   milestone; major bank crypto product; sanctions; "
    "                   $100M+ security incident against a known player.\n"
    "  70-89  high     : CIS legislation on digital assets (is_legislative"
    "                   =true + geo_priority=2); official regulator policy "
    "                   document or rule-making (NOT a routine fraud case); "
    "                   CIS regulator action; significant market infra "
    "                   change; established exchange (TOP-30) listing/"
    "                   licensing affecting KZ users; major KZ event "
    "                   (event_scale=kz_major).\n"
    "  50-69  medium   : expert commentary with named source, mid-tier "
    "                   product launch, regulatory clarification, notable "
    "                   tokenisation or DeFi case, major CIS event "
    "                   (event_scale=cis_major).\n"
    "  20-49  low      : minor token updates, generic market commentary, "
    "                   stale news repackaged, aggregator-reported small "
    "                   foreign enforcement (see below), minor events.\n"
    "   0-19  drop     : price predictions, promotional content, "
    "                   speculative rumours, near-duplicate of higher "
    "                   ranked items, events with event_scale=minor.\n"
    "\n"
    "Hard rules:\n"
    "  - geo_priority 1 articles get +1 step over an equivalent global "
    "    item, but never invent KZ relevance.\n"
    "  - is_legislative=true AND geo_priority in {1,2} => priority is at "
    "    LEAST high; for geo_priority=1 default to critical.\n"
    "  - topics contain 'events': priority follows event_scale "
    "    (kz_major=high, cis_major=medium, global_major=medium, "
    "    minor/null=drop).\n"
    "  - LOW-RELEVANCE FOREIGN ENFORCEMENT DOWNGRADE: when source is "
    "    NOT the official regulator (tier2_national / tier4_other) AND "
    "    the article reports a foreign-regulator action of ANY type "
    "    — fraud charges, sanctions, OFAC listings, civil suits, "
    "    asset freezes — against entities that are ALL of: "
    "    (a) NOT in the TOP-50 industry list (Binance, Coinbase, Kraken, "
    "    Bybit, OKX, Bitstamp, Gemini, Bitfinex, HTX, Tether, Circle, "
    "    MakerDAO, Paxos, BlockFi, Genesis, Celsius, Robinhood Crypto, "
    "    BlackRock, Fidelity, Grayscale, Ripple, Aave, Uniswap, Maker, "
    "    Lido, Curve, Compound); (b) NOT a stablecoin issuer or major "
    "    DeFi protocol; (c) operate primarily outside KZ and CIS — "
    "    typical targets: Iranian / North Korean / Venezuelan exchanges, "
    "    small private funds, third-country shells "
    "    => downgrade to LOW (score 15-30). These items don't change KZ "
    "    bank policy, don't reach KZ clients, don't move global markets. "
    "    Example cases that MUST be downgraded: SEC vs $12M Privvy fund; "
    "    OFAC sanctions on Nobitex/Wallex/Bitpin/Ramzinex (Iran); DOJ "
    "    indictments of unnamed dark-net operators. Cite this rule in "
    "    ranking_reason.\n"
    "  - prefer the original publisher over aggregators when scores tie.\n"
    "  - ids in ranked_articles MUST be from the supplied set; never "
    "    invent or transliterate identifiers.\n"
    "  - put low-signal duplicates in dropped_ids with a one-line reason.\n"
    "  - keep ranking_reason under 25 words; cite the rubric tier.\n"
    "\n"
    "Return JSON: {ranked_articles:[{id, priority, score, ranking_reason}],"
    " dropped_ids:[{id, reason}]}."
)

KZ_QUOTA_RATIO = 0.20
CIS_QUOTA_RATIO = 0.20
LEGISLATION_QUOTA_RATIO = 0.10
# Hard floor: at least one CIS story per digest regardless of size, when
# any CIS candidate exists in the pool. Bank stakeholders explicitly asked
# for visible CIS coverage even on quiet news days.
CIS_HARD_MIN = 1
KZ_HARD_MIN = 1


_TIER1_HINTS = (
    "afsa",
    "aifc",
    "nationalbank",
    "gov.kz",
    "ardfm",
    "mdai",
    "astanahub",
    "kase.kz",
)
_TIER2_HINTS = (
    "kapital",
    "kursiv",
    "forbes.kz",
    "forklog",
    "cbr.ru",
    "cbu.uz",
    "nbkr",
    "bits.media",
    "incrypted",
    "decenter",
    "coinspot.io",
    "habr.com",
    "profinance.kz",
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


# Source-id / URL substrings that identify a publication as CIS-based.
# Used by `_enforce_quotas` as a fallback when the classifier marked a CIS
# story as geo_priority=3 (common when the article body references US/EU
# entities even though the editorial lens is CIS). Distinct from
# `_TIER2_HINTS`: that tier mixes KZ media (forbes.kz, kapital.kz, kase.kz)
# which must NOT count toward the CIS quota — KZ already has its own slot.
_CIS_SOURCE_HINTS = (
    "forklog",
    "cbr.ru",
    "cbu.uz",
    "nbkr",
    "bits.media",
    "incrypted",
    "decenter",
    "coinspot.io",
    "habr.com",
)


def _is_cis_source(source_id: str, source_url: str) -> bool:
    haystack = f"{source_id} {source_url}".lower()
    return any(hint in haystack for hint in _CIS_SOURCE_HINTS)


class GeminiSkillPipeline:
    """Orchestrates the daily AI processing pipeline through Gemini-backed skills."""

    def __init__(
        self,
        llm: LlmClient,
        skill_loader: SkillLoader,
        *,
        process_concurrency: int = 5,
    ) -> None:
        self.llm = llm
        self.skill_loader = skill_loader
        self.process_concurrency = max(1, int(process_concurrency))

    def process_articles(self, articles: Iterable[RawArticle]) -> list[ProcessedArticle]:
        article_list = list(articles)
        if not article_list:
            return []
        # Sequential mode for dry-run / single-article calls avoids the
        # ThreadPoolExecutor overhead and keeps log ordering predictable.
        if self.process_concurrency == 1 or len(article_list) == 1:
            return self._process_sequential(article_list)
        return self._process_parallel(article_list)

    def _process_sequential(self, articles: list[RawArticle]) -> list[ProcessedArticle]:
        processed: list[ProcessedArticle] = []
        for article in articles:
            item = self._safe_process_one(article)
            if item is not None and item.geo_priority != 0 and item.topics:
                processed.append(item)
        return processed

    def _process_parallel(self, articles: list[RawArticle]) -> list[ProcessedArticle]:
        # Bounded ThreadPoolExecutor: each worker holds one Gemini request
        # in flight. 5 is a balance between throughput and Gemini's free-tier
        # per-minute rate limits.
        from concurrent.futures import ThreadPoolExecutor

        workers = min(self.process_concurrency, len(articles))
        results: list[ProcessedArticle | None] = [None] * len(articles)
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="crypto-process",
        ) as pool:
            for index, item in enumerate(
                pool.map(self._safe_process_one, articles)
            ):
                results[index] = item

        processed: list[ProcessedArticle] = []
        for item in results:
            if item is None:
                continue
            if item.geo_priority == 0 or not item.topics:
                logger.info("article_filtered article_id=%s reason=no_topics", item.id)
                continue
            processed.append(item)
        return processed

    def _safe_process_one(self, article: RawArticle) -> ProcessedArticle | None:
        try:
            return self.process_one(article)
        except Exception:
            logger.exception("article_processing_failed article_id=%s", article.id)
            return None

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
        # New v1.1 side-channel fields. All optional — classifier returns
        # them only when the underlying text supports the signal.
        result.is_legislative = bool(classified.get("is_legislative") or False)
        legislative_stage = classified.get("legislative_stage")
        if legislative_stage in {"introduced", "debated", "adopted", "signed", "in_force"}:
            result.legislative_stage = legislative_stage  # type: ignore[assignment]
        event_date = classified.get("event_date")
        if isinstance(event_date, str) and event_date:
            result.event_date = event_date
        event_location = classified.get("event_location")
        if isinstance(event_location, str) and event_location:
            result.event_location = event_location
        event_scale = classified.get("event_scale")
        if event_scale in {"kz_major", "cis_major", "global_major", "minor"}:
            result.event_scale = event_scale  # type: ignore[assignment]

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
                            "is_legislative": article.is_legislative,
                            "legislative_stage": article.legislative_stage,
                            "event_date": article.event_date,
                            "event_location": article.event_location,
                            "event_scale": article.event_scale,
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
        # Trim the payload sent to the QA skill: drop the full HTML (the
        # largest chunk, and the QA skill only needs the readable text),
        # drop telegram_segments (redundant with plain_text), and reduce
        # articles to the editorial-relevant fields. This keeps the prompt
        # well under Gemini's context window even for large digests.
        #
        # Crucially, `articles` here must reflect what is ACTUALLY in the
        # digest, not the full ranked set. Otherwise QA flags a structural
        # mismatch between e.g. "Публикаций в сводке: 1" in plain_text and
        # the longer articles array — exactly the false positive that hit
        # the live digest.
        rendered = _filter_articles_for_qa(digest, articles)
        payload = {
            "digest_date": digest.digest_date,
            "plain_text": digest.plain_text,
            "telegram_articles": [
                block.model_dump(mode="json") for block in digest.telegram_articles
            ],
            "stats": digest.stats,
            "articles": [
                {
                    "id": article.id,
                    "title": article.title_ru or article.title,
                    "summary": article.summary,
                    "source_name": article.source_name,
                    "source_url": article.source_url,
                    "priority": article.priority,
                    "topics": article.topics,
                    "country": article.country,
                    "geo_priority": article.geo_priority,
                    "has_image": bool(article.image_url),
                }
                for article in rendered
            ],
        }
        try:
            response = self.call_skill(
                "crypto-digest-quality-check",
                "QA this digest before delivery.",
                payload,
            )
            return QaResult.model_validate(response)
        except (JsonExtractionError, ValidationError, ValueError) as exc:
            logger.warning("quality_check_failed_using_permissive_qa error=%s", exc)
            return QaResult(
                passed=True,
                severity="warning",
                issues=[],
                warnings=[
                    {
                        "category": "qa_skill_failure",
                        "message": f"QA skill failed: {type(exc).__name__}",
                    }
                ],
                recommendation="send",
            )

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


def _filter_articles_for_qa(
    digest: Digest,
    articles: list[ProcessedArticle],
) -> list[ProcessedArticle]:
    """Restrict QA's `articles` view to publications actually in the digest.

    The QA skill performs a structural consistency check that cross-checks
    the per-section counts in plain_text against the length of the
    `articles` array. When the digest builder dropped items (tag filter,
    `total_max_items` cut, minor-event skip) we must hide those from QA so
    the check doesn't fire on a phantom mismatch.

    Primary signal: source_url of each `TelegramArticleBlock`. Fallback:
    when telegram_articles is empty (Gemini skill response without the
    field), trust `stats.total_articles` if it is a positive int smaller
    than `len(articles)`.
    """

    rendered_urls = {
        block.source_url for block in digest.telegram_articles if block.source_url
    }
    if rendered_urls:
        return [a for a in articles if a.source_url in rendered_urls]
    if isinstance(digest.stats, dict):
        total = digest.stats.get("total_articles")
        if isinstance(total, int) and 0 < total < len(articles):
            return articles[:total]
    return articles


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
    return _enforce_quotas(ranked, articles, total_max_items=total_max_items)


def _enforce_quotas(
    ranked: list[ProcessedArticle],
    candidates: list[ProcessedArticle],
    *,
    total_max_items: int,
) -> list[ProcessedArticle]:
    """Guarantee minimum slots for KZ / CIS / legislative content.

    The Gemini ranker can de-prioritise local stories when they share
    obvious markers with a louder global one. We enforce the audience
    contract here so a Kazakhstan-bank digest never silently degrades to
    a generic international feed when local material is available.
    """

    if total_max_items <= 0 or not ranked:
        return ranked[:total_max_items]

    kz_quota = max(KZ_HARD_MIN, int(total_max_items * KZ_QUOTA_RATIO))
    cis_quota = max(CIS_HARD_MIN, int(total_max_items * CIS_QUOTA_RATIO))
    leg_quota = max(1, int(total_max_items * LEGISLATION_QUOTA_RATIO))

    head = ranked[:total_max_items]
    head_ids = {article.id for article in head}

    def pool(predicate: Any) -> list[ProcessedArticle]:
        return [a for a in sorted(candidates, key=article_sort_key) if predicate(a)]

    def boost(predicate: Any, quota: int) -> None:
        current = sum(1 for a in head if predicate(a))
        if current >= quota:
            return
        for candidate in pool(predicate):
            if candidate.id in head_ids:
                continue
            head.append(candidate)
            head_ids.add(candidate.id)
            current += 1
            if current >= quota:
                break

    def is_cis(article: ProcessedArticle) -> bool:
        # Primary signal: classifier-assigned geo_priority. Fallback:
        # known CIS publisher even when the classifier flagged the
        # article as geo_priority=3 (typical for forklog/bits stories
        # that mention foreign entities but cover CIS jurisdiction).
        if article.geo_priority == 2:
            return True
        if article.geo_priority == 1:
            return False  # KZ has its own quota; do not double-count.
        return _is_cis_source(article.source_id, article.source_url)

    boost(lambda a: a.is_legislative and a.geo_priority in {1, 2}, leg_quota)
    boost(lambda a: a.geo_priority == 1, kz_quota)
    boost(is_cis, cis_quota)

    if len(head) <= total_max_items:
        return head

    # Reductions: prefer to drop excess geo_priority=3 medium/low entries
    # rather than the quota-protected ones we just inserted.
    protected: set[str] = set()
    seen_protected: dict[str, int] = {"leg": 0, "kz": 0, "cis": 0}
    for article in head:
        if article.is_legislative and article.geo_priority in {1, 2} \
                and seen_protected["leg"] < leg_quota:
            protected.add(article.id)
            seen_protected["leg"] += 1
        elif article.geo_priority == 1 and seen_protected["kz"] < kz_quota:
            protected.add(article.id)
            seen_protected["kz"] += 1
        elif is_cis(article) and seen_protected["cis"] < cis_quota:
            protected.add(article.id)
            seen_protected["cis"] += 1

    trimmed: list[ProcessedArticle] = []
    overflow: list[ProcessedArticle] = []
    for article in head:
        if article.id in protected:
            trimmed.append(article)
        else:
            overflow.append(article)
    overflow.sort(key=article_sort_key)
    while len(trimmed) < total_max_items and overflow:
        trimmed.append(overflow.pop(0))
    return trimmed[:total_max_items]


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
