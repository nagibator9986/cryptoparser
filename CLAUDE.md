# Crypto Monitor Gemini — guide for Claude

This file is loaded into every Claude session in this repo. Read it before
touching anything. It encodes hard-won decisions about what the project is,
how it is shaped, and what is intentionally out of scope.

## What this project is

Automated daily monitoring of digital-asset news for a Kazakhstan-based bank.
Sources → AI pipeline (Google Gemini) → ranked digest with images → Telegram
group at 09:00 Asia/Almaty.

**Out of scope by user request:** email delivery (SMTP code is still here for
backwards compat but is not the active product surface). Do not add email
features unless explicitly asked.

The spec is [`ТЗ_Мониторинг_цифровых_активов.docx`](ТЗ_Мониторинг_цифровых_активов.docx)
and the gap analysis is [`docs/tz-gap-analysis.md`](docs/tz-gap-analysis.md).
Update the gap analysis whenever a requirement status changes.

## Architecture in 30 seconds

```
collectors (RSS / HTML / JSON_API)
       │  RawArticle (+ image_url, author)
       ▼
normalization (Asia/Almaty TZ, ru/en/kk language detect, image URL guard)
       │
       ▼ sanitize_untrusted_text  ← runs BEFORE every Gemini call
       │
       ▼
Gemini Skill Pipeline (crypto-monitor-skills/*)
   translator → classifier → summarizer → prioritizer
   → deduplicator → cross-article ranker → digest-builder → quality-check
       │
       ▼
SQLite (data/crypto_monitor.sqlite3)
       │
       ▼
Telegram delivery
   header text → per-section header → per-article sendPhoto (with image)
   or sendMessage (no image)  → footer text
```

Skills are *external prompt modules* in [`crypto-monitor-skills/`](crypto-monitor-skills/).
Each is a directory with `SKILL.md` + `references/` + `evals/evals.json`. Code
loads them through [`SkillLoader`](crypto_monitor/skills.py) — never duplicate
their content in Python.

## File map (what to touch where)

| Concern | File | Notes |
|---|---|---|
| Settings, env, paths | [crypto_monitor/config.py](crypto_monitor/config.py) | pydantic-settings; `.env` loader |
| Data models | [crypto_monitor/models.py](crypto_monitor/models.py) | All Pydantic v2; `RawArticle.image_url` flows end-to-end |
| TZ, language, image URL guards | [crypto_monitor/normalization.py](crypto_monitor/normalization.py) | `normalize_image_url` rejects data: URIs and tracking pixels |
| Untrusted text guard | [crypto_monitor/security.py](crypto_monitor/security.py) | Apply before every LLM call — never skip |
| Collectors | [crypto_monitor/collectors/](crypto_monitor/collectors/) | feedparser for RSS, BeautifulSoup for HTML, plain JSON otherwise |
| Skill loader | [crypto_monitor/skills.py](crypto_monitor/skills.py) | Reads `SKILL.md` + references; do not inline prompts |
| Gemini client | [crypto_monitor/gemini.py](crypto_monitor/gemini.py) | JSON mode + JSON-repair retry; `DryRunLlmClient` for tests |
| Pipeline orchestration | [crypto_monitor/pipeline.py](crypto_monitor/pipeline.py) | Cross-article ranking lives here; see `RANKING_TASK` constant |
| Digest renderer (fallback) | [crypto_monitor/digest_renderer.py](crypto_monitor/digest_renderer.py) | Builds `telegram_articles` blocks consumed by delivery |
| Telegram delivery | [crypto_monitor/delivery/telegram.py](crypto_monitor/delivery/telegram.py) | `sendPhoto` per article when `image_url` present |
| Telegram command bot | [crypto_monitor/telegram_bot.py](crypto_monitor/telegram_bot.py) | Long polling, inline menus, callback queries, RBAC |
| Storage | [crypto_monitor/storage.py](crypto_monitor/storage.py) | SQLite; JSON payload columns — schema-flexible |
| Health endpoint | [crypto_monitor/health.py](crypto_monitor/health.py) | `/health`, `/live` — Railway readiness gate |
| CLI | [crypto_monitor/cli.py](crypto_monitor/cli.py) | Typer; commands: collect/process/digest/run/evals/telegram-bot/railway |
| Eval runner | [crypto_monitor/evals.py](crypto_monitor/evals.py) | NL-DSL for skill assertion checks |

## Conventions (binding)

### Code style

- Python 3.11+. `from __future__ import annotations` at the top of every module.
- pydantic v2 models everywhere. No dataclasses for data carriers (only for
  internal scratch like `EvalCaseResult`).
- Ruff is the only style checker. Lint must be clean (`ruff check`).
- Type hints throughout. Use `| None` not `Optional[...]`. Use `list[X]` not
  `List[X]`. Use `datetime.UTC` not `timezone.utc`.
- No emoji in code or output unless the user explicitly asks for it.
- No comments that just restate what the code does. Only comment hidden
  invariants, non-obvious workarounds, or rationale that would surprise a
  future reader.
- No docstrings on small helpers. Module-level classes and complex functions
  get one short paragraph max.

### Architecture rules

- **Skills are prompt modules, not Python code.** Never inline a skill's
  system prompt. Always go through `SkillLoader`.
- **`LlmClient` is a Protocol.** Tests use `DryRunLlmClient`; runtime uses
  `GeminiClient`. Never call the Gemini SDK directly from anywhere except
  `crypto_monitor/gemini.py`.
- **Sanitise before every LLM call.** `sanitize_untrusted_text` runs in
  `pipeline.process_one` for title/body. Preserve `original_title` /
  `original_body` for audit.
- **All datetimes are TZ-aware.** Use `normalize_datetime` /
  `digest_date_or_previous_day`. The default user-facing TZ is Asia/Almaty.
- **Image URLs are validated.** Run every candidate URL through
  `normalize_image_url`. Never push a URL to Telegram that hasn't been through
  this guard — data: URIs and tracking pixels will fail the API or leak
  signals.
- **One Telegram bot token = one polling consumer.** 409 Conflict from
  `getUpdates` means a second consumer exists somewhere. The right fix is
  ops (revoke / remove duplicate replica), not code retries.

### Testing

- `python3 -m pytest -q` must pass. Currently 51 tests; do not regress.
- `crypto-monitor evals --dry-run` must pass. Currently 100% on all 8 skills.
- `ruff check crypto_monitor tests` must be clean.
- Real integration tests against Gemini are not part of the default suite —
  do not add network calls to the unit tests.
- Tests must NOT mock `LlmClient` with `MagicMock`. Use `DryRunLlmClient` or
  a hand-written fake that returns the JSON shape the skill specifies.
- For Telegram bot tests, use `FakeTelegramApi` from
  [tests/test_telegram_bot.py](tests/test_telegram_bot.py). It mirrors the
  real API surface including `edit_message_text`, `send_message`,
  `answer_callback_query`.

### What NOT to do

- Do not add SMTP/email features.
- Do not import `feedparser` or `bs4` outside the collectors directory.
- Do not introduce new persistence layers (Redis, Postgres) without an explicit
  request — the gap analysis lists them as production backlog, not MVP.
- Do not add a web admin. Group settings live in Telegram commands per
  user decision.
- Do not call Telegram API directly from places other than
  [crypto_monitor/delivery/telegram.py](crypto_monitor/delivery/telegram.py)
  and [crypto_monitor/telegram_bot.py](crypto_monitor/telegram_bot.py).
- Do not add inline buttons to `Digest` content (article cards). The bot's
  inline keyboards are for chat administration only; article blocks must
  stay clean to support sendPhoto captions.

## Daily-use commands

```bash
# Verify state
python3 -m pytest -q
ruff check crypto_monitor tests
crypto-monitor evals --dry-run

# Local pipeline against bundled sample (no API key needed)
crypto-monitor run --input-json sample_articles.json --dry-run

# Real pipeline (needs GEMINI_API_KEY in .env)
crypto-monitor collect
crypto-monitor process
crypto-monitor digest --date 2026-05-29

# Bot in a group (needs TELEGRAM_BOT_TOKEN)
crypto-monitor telegram-bot

# Railway-equivalent (bot + /health on $PORT)
crypto-monitor railway
```

## Ranking — how it works

`pipeline.rank_articles_for_digest` is the cross-article quality gate. It
sends the entire candidate set to `crypto-news-prioritizer` with the
`RANKING_TASK` rubric. Key behaviours:

- `source_authority` is pre-computed from URL/source-id substring matching
  and supplied to the model. Tier1 = KZ regulators, Tier2 = national media
  & CIS regulators, Tier3 = major international crypto media, Tier4 = other.
- `has_image` is signalled so the model can favour editorial content over
  text-only blog posts when ties happen.
- Output contract: `{ranked_articles: [{id, priority, score, ranking_reason}],
  dropped_ids: [{id, reason}]}`. Legacy `dropped_ids: [str]` form is still
  accepted by `apply_ranking_response` for backwards compatibility with the
  dry-run client.
- Local sort fallback runs on Gemini failure (`article_sort_key`).

If you tune the rubric, also update `DryRunLlmClient` in
[crypto_monitor/gemini.py](crypto_monitor/gemini.py) so dry-run continues to
return realistic shapes.

## Images — how they flow

1. **Collectors** extract images:
   - RSS: enclosures, `media:content`, `media:thumbnail`, `itunes:image`,
     `<image>`, inline `<img>` in `content:encoded`.
   - HTML: `og:image`, `og:image:secure_url`, `twitter:image`, `link[rel=image_src]`,
     JSON-LD `image`, first sensible `<img>` in `<article>`.
   - JSON API: common keys (`image`, `image_url`, `thumbnail`, `cover_image`).
2. **Validation**: `normalize_image_url` runs on each candidate, rejects data:
   URIs, tracking pixels, and unresolved relative paths.
3. **Storage**: `RawArticle.image_url` and `image_urls: list[str]` are stored
   in `raw_articles.payload` (JSON column — no migration needed).
4. **Pipeline**: image fields pass through unchanged. Sanitiser does not
   touch image URLs.
5. **Digest**: `digest_renderer._build_telegram_articles` produces
   `Digest.telegram_articles: list[TelegramArticleBlock]`.
6. **Delivery**: `TelegramDelivery._send_article` calls `sendPhoto` when
   `image_url` is present, with MarkdownV2 caption ≤1024 chars. On 4xx the
   image is dropped and the article is sent as plain text instead.

If Telegram rejects an image with 400, that's the publisher's fault (referer
blocking, expired URL). The fallback to text is silent — log the warning,
do not retry the same URL.

## Production gaps (intentional, do not "fix" without asking)

These are listed in [docs/tz-gap-analysis.md](docs/tz-gap-analysis.md). Touch
them only when the user asks.

- PostgreSQL migration
- Celery / RQ workers, Redis
- Telegram/X MTProto ingestion
- Source-specific HTML parser plugins
- Playwright for JS-rendered pages
- FTS index in storage
- Web admin
- Approval workflow for digests
- Prometheus / Grafana / alertmanager
- Vault for secrets
