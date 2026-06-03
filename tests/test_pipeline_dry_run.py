from pathlib import Path

from crypto_monitor.gemini import DryRunLlmClient
from crypto_monitor.models import ProcessedArticle, RawArticle
from crypto_monitor.pipeline import (
    GeminiSkillPipeline,
    _enforce_quotas,
    apply_ranking_response,
    deduplicate_exact,
)
from crypto_monitor.skills import SkillLoader


def test_pipeline_dry_run_end_to_end() -> None:
    article = RawArticle(
        id="a1",
        source_id="manual",
        source_name="nationalbank.kz",
        source_url="https://nationalbank.kz/test",
        title="НБРК объявил о развитии цифрового тенге",
        body="Национальный банк Казахстана сообщил о развитии проекта цифрового тенге.",
        language="ru",
    )
    pipeline = GeminiSkillPipeline(DryRunLlmClient(), SkillLoader(Path("crypto-monitor-skills")))
    articles, digest, qa = pipeline.run([article], digest_date="2026-05-24")
    assert len(articles) == 1
    assert digest.html
    assert qa.recommendation == "send"


def test_exact_deduplication_removes_same_url_with_tracking_params() -> None:
    first = _processed("a1", "https://example.com/news?utm_source=x")
    second = _processed("a2", "https://example.com/news")

    assert [article.id for article in deduplicate_exact([first, second])] == ["a1"]


def test_apply_ranking_response_updates_order_priority_and_score() -> None:
    first = _processed("a1", "https://example.com/1")
    second = _processed("a2", "https://example.com/2")

    ranked = apply_ranking_response(
        [first, second],
        {
            "ranked_articles": [
                {
                    "id": "a2",
                    "priority": "critical",
                    "score": 96,
                    "ranking_reason": "More important for KZ bank.",
                }
            ],
            "dropped_ids": [],
        },
        total_max_items=2,
    )

    assert [article.id for article in ranked] == ["a2", "a1"]
    assert ranked[0].priority == "critical"
    assert ranked[0].score == 96
    assert ranked[0].ranking_reason == "More important for KZ bank."


def test_quota_inserts_kz_article_pushed_out_by_global_news() -> None:
    """When Gemini drops all KZ entries, the quota must bring them back."""

    kz = _processed("kz1", "https://nationalbank.kz/1")
    cis = _processed("cis1", "https://forklog.com/1", country="RU", geo_priority=2)
    intl_a = _processed("int_a", "https://coindesk.com/a", country="US", geo_priority=3)
    intl_b = _processed("int_b", "https://coindesk.com/b", country="US", geo_priority=3)
    intl_c = _processed("int_c", "https://coindesk.com/c", country="US", geo_priority=3)

    # Gemini returns only the three international items — KZ and CIS are absent.
    ranked = apply_ranking_response(
        [kz, cis, intl_a, intl_b, intl_c],
        {
            "ranked_articles": [
                {"id": "int_a", "priority": "high", "score": 80},
                {"id": "int_b", "priority": "high", "score": 75},
                {"id": "int_c", "priority": "high", "score": 70},
            ],
            "dropped_ids": ["kz1", "cis1"],
        },
        total_max_items=5,
    )

    # int_* are dropped_ids, so they should not appear; the quota must
    # surface KZ + CIS even though Gemini explicitly dropped them.
    ids = [article.id for article in ranked]
    assert "kz1" in ids, f"KZ quota must guarantee inclusion, got {ids}"
    assert "cis1" in ids, f"CIS quota must guarantee inclusion, got {ids}"


def test_quota_protects_legislation_from_truncation() -> None:
    """Legislative KZ news must survive the total_max_items cut."""

    leg = _processed("leg1", "https://gov.kz/1", country="KZ", geo_priority=1)
    leg.is_legislative = True
    leg.legislative_stage = "introduced"
    fillers = [
        _processed(
            f"fill{i}",
            f"https://coindesk.com/{i}",
            country="US",
            geo_priority=3,
        )
        for i in range(10)
    ]

    head = [*fillers, leg]
    result = _enforce_quotas(head, [*fillers, leg], total_max_items=3)
    assert "leg1" in [article.id for article in result]


def _processed(
    article_id: str,
    source_url: str,
    *,
    country: str = "KZ",
    geo_priority: int = 1,
) -> ProcessedArticle:
    return ProcessedArticle(
        id=article_id,
        source_id="src",
        source_name="Source",
        source_url=source_url,
        title="AFSA выдало лицензию криптопровайдеру",
        body="AFSA сообщило о лицензировании поставщика услуг цифровых активов.",
        language="ru",
        topics=["regulation"],
        country=country,
        geo_priority=geo_priority,
        priority="high",
        score=80,
    )
