from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from crypto_monitor.collector_runner import CollectorRunner
from crypto_monitor.config import get_settings
from crypto_monitor.delivery.emailer import EmailDelivery
from crypto_monitor.delivery.telegram import TelegramDelivery
from crypto_monitor.evals import SkillEvalRunner
from crypto_monitor.gemini import DryRunLlmClient, GeminiClient
from crypto_monitor.health import start_health_server
from crypto_monitor.logging import configure_logging
from crypto_monitor.models import RawArticle
from crypto_monitor.normalization import digest_date_or_previous_day
from crypto_monitor.pipeline import GeminiSkillPipeline
from crypto_monitor.skills import SkillLoader
from crypto_monitor.sources import load_sources
from crypto_monitor.storage import SqliteStorage
from crypto_monitor.telegram_bot import TelegramCommandBot

app = typer.Typer(help="Crypto Monitor pipeline powered by Google Gemini.")
console = Console()


def build_pipeline(dry_run: bool = False) -> GeminiSkillPipeline:
    settings = get_settings()
    llm = (
        DryRunLlmClient()
        if dry_run
        else GeminiClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            max_retries=settings.gemini_max_retries,
        )
    )
    return GeminiSkillPipeline(
        llm=llm,
        skill_loader=SkillLoader(settings.skills_root),
        process_concurrency=settings.process_concurrency,
    )


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    configure_logging()
    if verbose:
        console.print("[dim]Verbose logging enabled.[/dim]")


@app.command()
def skills() -> None:
    """List available prompt skills."""

    settings = get_settings()
    loader = SkillLoader(settings.skills_root)
    table = Table(title="Available skills")
    table.add_column("Skill")
    for name in loader.list_skills():
        table.add_row(name)
    console.print(table)


@app.command()
def collect(
    limit_per_source: Annotated[int, typer.Option("--limit-per-source", "-l")] = 10,
) -> None:
    """Collect raw articles from configured sources and save them."""

    settings = get_settings()
    storage = SqliteStorage(settings.db_path)
    sources = load_sources(settings.sources_file)
    articles = CollectorRunner().collect_all(
        sources,
        limit_per_source=limit_per_source,
        status_recorder=storage,
        concurrency=settings.collect_concurrency,
    )
    saved = storage.save_raw_articles(articles)
    storage.log_event("collect", {"collected": len(articles), "saved": saved})
    console.print(f"Collected {len(articles)} articles, saved {saved} new articles.")


@app.command()
def process(
    limit: Annotated[int, typer.Option("--limit", "-l")] = 50,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Process stored raw articles through Gemini skills."""

    settings = get_settings()
    storage = SqliteStorage(settings.db_path)
    raw_articles = storage.load_raw_articles(limit=limit)
    pipeline = build_pipeline(dry_run=dry_run)
    processed = pipeline.process_articles(raw_articles)
    canonical = pipeline.deduplicate(processed)
    storage.save_processed_articles(canonical)
    storage.log_event(
        "process",
        {"raw": len(raw_articles), "processed": len(processed), "canonical": len(canonical)},
    )
    console.print(
        f"Processed {len(raw_articles)} raw articles, "
        f"saved {len(canonical)} canonical articles."
    )


@app.command()
def digest(
    digest_date: Annotated[str | None, typer.Option("--date")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l")] = 25,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    send_email: Annotated[list[str] | None, typer.Option("--email")] = None,
    send_telegram: Annotated[bool, typer.Option("--telegram")] = False,
) -> None:
    """Build and QA a digest from processed articles."""

    settings = get_settings()
    storage = SqliteStorage(settings.db_path)
    effective_date = digest_date_or_previous_day(digest_date)
    articles = storage.load_processed_articles_for_digest(effective_date, limit=limit)
    pipeline = build_pipeline(dry_run=dry_run)
    ranked = pipeline.rank_articles_for_digest(
        articles,
        digest_date=effective_date,
        total_max_items=limit,
    )
    digest_result = pipeline.build_digest(ranked, digest_date=effective_date)
    qa = pipeline.quality_check(digest_result, ranked)
    storage.save_digest(digest_result)
    storage.log_event(
        "digest",
        {
            "digest_date": digest_result.digest_date,
            "articles": len(ranked),
            "qa": qa.model_dump(mode="json"),
        },
    )

    console.print(
        f"Digest {digest_result.digest_date}: "
        f"QA={qa.recommendation}, severity={qa.severity}"
    )
    console.print(digest_result.plain_text[:2000])

    if send_email:
        EmailDelivery(settings).send(digest_result, list(send_email))
        storage.log_event(
            "delivery.email",
            {"digest_date": digest_result.digest_date, "recipients": len(send_email)},
        )
        console.print(f"Email sent to {len(send_email)} recipients.")
    if send_telegram:
        TelegramDelivery(settings).send(digest_result)
        storage.log_event("delivery.telegram", {"digest_date": digest_result.digest_date})
        console.print("Telegram digest sent.")


@app.command()
def run(
    input_json: Annotated[Path | None, typer.Option("--input-json")] = None,
    digest_date: Annotated[str | None, typer.Option("--date")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Run collect/process/digest pipeline.

    If --input-json is provided, the file must contain an array of RawArticle-compatible objects.
    Otherwise, the command collects from configured RSS/HTML sources.
    """

    settings = get_settings()
    storage = SqliteStorage(settings.db_path)
    if input_json:
        data = json.loads(input_json.read_text(encoding="utf-8"))
        raw_articles = [RawArticle.model_validate(item) for item in data]
        storage.save_raw_articles(raw_articles)
    else:
        sources = load_sources(settings.sources_file)
        raw_articles = CollectorRunner().collect_all(
            sources,
            status_recorder=storage,
            concurrency=settings.collect_concurrency,
        )
        storage.save_raw_articles(raw_articles)

    pipeline = build_pipeline(dry_run=dry_run)
    effective_date = digest_date_or_previous_day(digest_date)
    articles, digest_result, qa = pipeline.run(raw_articles, digest_date=effective_date)
    storage.save_processed_articles(articles)
    storage.save_digest(digest_result)
    storage.log_event(
        "run",
        {
            "dry_run": dry_run,
            "raw": len(raw_articles),
            "processed": len(articles),
            "digest_date": digest_result.digest_date,
            "qa": qa.model_dump(mode="json"),
        },
    )
    console.print(
        {
            "raw": len(raw_articles),
            "processed": len(articles),
            "digest_date": digest_result.digest_date,
            "qa": qa.model_dump(mode="json"),
        }
    )


@app.command()
def validate_source(
    source_json: Annotated[Path, typer.Argument(help="Path to JSON candidate source payload")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Evaluate whether a new source should be added to the catalog."""

    candidate = json.loads(source_json.read_text(encoding="utf-8"))
    result = build_pipeline(dry_run=dry_run).validate_source(candidate)
    console.print_json(data=result)


@app.command()
def status() -> None:
    """Show local storage status."""

    settings = get_settings()
    console.print_json(data=SqliteStorage(settings.db_path).export_json())


@app.command("sources-status")
def sources_status(limit: Annotated[int, typer.Option("--limit", "-l")] = 100) -> None:
    """Show source collection health and recent errors."""

    settings = get_settings()
    console.print_json(data=SqliteStorage(settings.db_path).list_source_statuses(limit=limit))


@app.command("search")
def search_archive(
    query: Annotated[str, typer.Argument(help="Text to search in articles and digests")],
    kind: Annotated[str, typer.Option("--kind", "-k")] = "all",
    limit: Annotated[int, typer.Option("--limit", "-l")] = 20,
    source_id: Annotated[str | None, typer.Option("--source")] = None,
    topic: Annotated[str | None, typer.Option("--topic")] = None,
    country: Annotated[str | None, typer.Option("--country")] = None,
) -> None:
    """Search archived raw articles, processed articles, and digests."""

    if kind not in {"all", "raw", "processed", "articles", "digests"}:
        raise typer.BadParameter("kind must be one of: all, raw, processed, articles, digests")
    settings = get_settings()
    console.print_json(
        data=SqliteStorage(settings.db_path).search_archive(
            query,
            kind=kind,
            limit=limit,
            source_id=source_id,
            topic=topic,
            country=country,
        )
    )


@app.command("digests")
def list_digests(limit: Annotated[int, typer.Option("--limit", "-l")] = 20) -> None:
    """List archived digests."""

    settings = get_settings()
    console.print_json(data=SqliteStorage(settings.db_path).list_digests(limit=limit))


@app.command("show-digest")
def show_digest(
    digest_date: Annotated[str, typer.Argument(help="Digest date, e.g. 2026-05-24")],
    fmt: Annotated[str, typer.Option("--format", "-f")] = "plain",
) -> None:
    """Show an archived digest in plain/html/telegram/json format."""

    settings = get_settings()
    digest = SqliteStorage(settings.db_path).load_digest(digest_date)
    if not digest:
        raise typer.BadParameter(f"Digest not found: {digest_date}")
    if fmt == "plain":
        console.print(digest.plain_text)
    elif fmt == "html":
        console.print(digest.html)
    elif fmt == "telegram":
        console.print("\n\n---\n\n".join(digest.telegram_segments))
    elif fmt == "json":
        console.print_json(data=digest.model_dump(mode="json"))
    else:
        raise typer.BadParameter("format must be one of: plain, html, telegram, json")


@app.command("audit")
def audit(limit: Annotated[int, typer.Option("--limit", "-l")] = 50) -> None:
    """Show audit events."""

    settings = get_settings()
    console.print_json(data=SqliteStorage(settings.db_path).list_audit_events(limit=limit))


@app.command("telegram-bot")
def telegram_bot(
    poll_timeout: Annotated[int, typer.Option("--poll-timeout")] = 30,
    poll_interval: Annotated[float, typer.Option("--poll-interval")] = 1.0,
    once: Annotated[bool, typer.Option("--once")] = False,
) -> None:
    """Run Telegram long-polling bot for group commands and scheduled digests."""

    settings = get_settings()
    storage = SqliteStorage(settings.db_path)
    bot = TelegramCommandBot(
        settings=settings,
        storage=storage,
        pipeline_factory=build_pipeline,
    )
    if once:
        bot.prepare_long_polling()
        next_offset = bot.poll_once(timeout=poll_timeout)
        console.print(f"Telegram poll completed. next_offset={next_offset}")
        return
    console.print("Telegram bot polling started. Press Ctrl+C to stop.")
    bot.run_forever(poll_timeout=poll_timeout, poll_interval=poll_interval)


@app.command("railway")
def railway_service(
    poll_timeout: Annotated[int, typer.Option("--poll-timeout")] = 30,
    poll_interval: Annotated[float, typer.Option("--poll-interval")] = 1.0,
    health_host: Annotated[str, typer.Option("--health-host")] = "0.0.0.0",
    health_port: Annotated[int | None, typer.Option("--health-port")] = None,
) -> None:
    """Run Railway service: Telegram bot plus HTTP health endpoint."""

    settings = get_settings()
    health_server = start_health_server(settings, host=health_host, port=health_port)
    health_thread = threading.Thread(
        target=health_server.serve_forever,
        name="health-server",
        daemon=True,
    )
    health_thread.start()

    storage = SqliteStorage(settings.db_path)
    bot = TelegramCommandBot(
        settings=settings,
        storage=storage,
        pipeline_factory=build_pipeline,
    )
    console.print("Railway service started: Telegram polling + /health endpoint.")
    try:
        bot.run_forever(poll_timeout=poll_timeout, poll_interval=poll_interval)
    finally:
        health_server.shutdown()
        health_server.server_close()


@app.command("telegram-chats")
def telegram_chats() -> None:
    """List Telegram group settings saved by the command bot."""

    settings = get_settings()
    chats = [
        chat.model_dump(mode="json")
        for chat in SqliteStorage(settings.db_path).list_telegram_chat_settings()
    ]
    console.print_json(data=chats)


@app.command("evals")
def run_evals(
    skill_name: Annotated[str | None, typer.Option("--skill")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Run skill evals through Gemini and check machine-readable assertions."""

    settings = get_settings()
    llm = (
        DryRunLlmClient()
        if dry_run
        else GeminiClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            max_retries=settings.gemini_max_retries,
        )
    )
    runner = SkillEvalRunner(settings.skills_root, llm)
    suites = [runner.run_skill(skill_name)] if skill_name else runner.run_all()
    SqliteStorage(settings.db_path).log_event(
        "evals",
        {
            "skill": skill_name or "*",
            "dry_run": dry_run,
        },
    )

    table = Table(title="Skill evals")
    table.add_column("Skill")
    table.add_column("Passed")
    table.add_column("Total")
    table.add_column("Rate")
    table.add_column("Failures")
    failed = False
    for suite in suites:
        failures = sum(len(case.failures) for case in suite.cases)
        failed = failed or failures > 0
        table.add_row(
            suite.skill_name,
            str(suite.passed),
            str(suite.total),
            f"{suite.pass_rate:.0%}",
            str(failures),
        )
    console.print(table)

    for suite in suites:
        for case in suite.cases:
            if case.failures:
                console.print(f"[red]{suite.skill_name} #{case.id} {case.name}[/red]")
                for failure in case.failures:
                    console.print(f"  - {failure}")

    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
