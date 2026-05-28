from pathlib import Path

from crypto_monitor.gemini import DryRunLlmClient
from crypto_monitor.models import ProcessedArticle, RawArticle
from crypto_monitor.pipeline import (
    GeminiSkillPipeline,
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
    assert any("Ranking:" in warning for warning in ranked[0].warnings)


def _processed(article_id: str, source_url: str) -> ProcessedArticle:
    return ProcessedArticle(
        id=article_id,
        source_id="src",
        source_name="Source",
        source_url=source_url,
        title="AFSA выдало лицензию криптопровайдеру",
        body="AFSA сообщило о лицензировании поставщика услуг цифровых активов.",
        language="ru",
        topics=["regulation"],
        country="KZ",
        geo_priority=1,
        priority="high",
        score=80,
    )
