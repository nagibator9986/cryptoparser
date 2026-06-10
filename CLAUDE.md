# Crypto Monitor Gemini — guide for Claude

This file is loaded into every Claude session in this repo. Read it before
touching anything. It encodes hard-won decisions about what the project is,
how it is shaped, and what is intentionally out of scope.

## What this project is

Automated daily monitoring of digital-asset news for a Kazakhstan-based bank.
Sources → AI pipeline (Google Gemini) → ranked digest with images → Telegram
group at 09:00 Asia/Almaty. Immediately after the digest, the same scheduler
posts the official KGD digital-asset rates (see "Rates (KGD)" below).

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
| Collectors | [crypto_monitor/collectors/](crypto_monitor/collectors/) | feedparser for RSS, BeautifulSoup for HTML, plain JSON otherwise. HTML supports `html_list` mode (per-article extraction from news-list pages) |
| gov.kz collector | [crypto_monitor/collectors/gov_kz.py](crypto_monitor/collectors/gov_kz.py) | `type: gov_kz`; queries the gov.kz Apollo GraphQL `news` API per entity |
| KGD rates collector | [crypto_monitor/collectors/kgd_rates.py](crypto_monitor/collectors/kgd_rates.py) | Scrapes qoldau.kz table; `parse_rates_html` is a pure, testable fn |
| Rates domain logic | [crypto_monitor/rates.py](crypto_monitor/rates.py) | Attribution constant, formatting, fetch+store+fallback. No bs4, no LLM |
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

- `python3 -m pytest -q` must pass. Currently 93 tests; do not regress.
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
- Do not route KGD rates through the Gemini pipeline — they are reference
  numbers, not articles. Keep `RATES_ATTRIBUTION` verbatim and always show
  the qoldau source link. Publish only the approved-11 coins.
- Do not enable `binance-news` until a source-specific collector exists (see
  "Binance News — assessed" above).

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

## Rates (KGD) — how they flow

A second, deliberately separate deliverable from the news digest: the official
daily digital-asset prices published by the Kazakhstan State Revenue Committee
(КГД). This is **numeric reference data, not news** — it bypasses the Gemini
pipeline entirely (no translate / classify / summarise / rank). Treating a
price table as an article would waste tokens and invent structure.

1. **Source reality.** kgd.gov.kz publishes no feed; the live data lives on the
   КГД token portal [token.qoldau.kz](https://token.qoldau.kz/ru/references/crypto-currency/list).
   It has **no API, no export** — only a server-rendered HTML table with a
   multiselect currency filter (`flCryptoCurrencyType`, repeated GET params),
   a date range, and `?p=` pagination (~20k rows).
2. **Approved-11 only.** We publish exactly the official approved list
   (`APPROVED_COINS` in [collectors/kgd_rates.py](crypto_monitor/collectors/kgd_rates.py)):
   BTC, ETC, BCH, LTC, XMR, ZEC, DASH, TRX, DOGE, ZEN, SC. Filtering all 11 in
   one GET returns ≤15 rows; we take the most recent date present, which is the
   previous day (T-1). Do not add coins the customer did not approve.
3. **Number format.** qoldau uses nbsp/space thousands and comma decimals
   (`32 632 058,050`). `_parse_kz_decimal` handles this. Faithful to source:
   sub-cent coins (Siacoin) are rounded to `0.000` KZT by КГД — we report the
   official `0`, we do not fabricate a finer value.
4. **USD.** КГД publishes only the KZT price + the USD/KZT reference rate.
   `price_usd = price_kzt / usd_kzt` is computed locally and labelled as
   derived. The customer asked for both KZT and USD.
5. **Attribution is verbatim and mandatory.** `RATES_ATTRIBUTION` in
   [rates.py](crypto_monitor/rates.py) — "Согласно публикации КГД, стоимость
   криптовалюты на основании данных за предыдущие сутки." Never paraphrase it.
   Every rates message also links the qoldau source.
6. **Delivery.** `TelegramDelivery.send_rates` renders one MarkdownV2 message
   (`render_rates_markdown_v2`), with a plain-text fallback on a 400. In the
   scheduler, `_maybe_send_scheduled_rates` runs **after** `_send_digest`,
   gated by the per-chat `send_rates` flag, wrapped so a rates outage never
   blocks the news digest. `get_rates_with_fallback` serves the last stored
   snapshot (with its own date, so the T-1 wording stays true) when the live
   fetch fails.
7. **Surfaces.** CLI `crypto-monitor rates [--telegram]`; bot `/crypto_rates`
   (read-only, any member); `/crypto_set send_rates on|off`. Snapshots persist
   in the `crypto_rates` table (`save_rates_snapshot` / `load_latest_rates_snapshot`).

`parse_rates_html` is a pure function so the parser is unit-tested against a
fixture, never the live network. If qoldau changes its table markup, that test
is where the breakage surfaces; the `source_status` monitor surfaces fetch
outages in production.

## KZ coverage — collector, window, language (why the digest now has KZ)

A live end-to-end run exposed that the digest could contain **zero Kazakhstan
content** despite KZ being the whole point. Three ingestion fixes restored it:

1. **`html_list` mode** ([collectors/html.py](crypto_monitor/collectors/html.py)).
   KZ regulator pages (nationalbank, afsa, aifc, kase, forbes) are news *lists*,
   not articles. The old single-article path scraped the landing-page chrome
   ("Пресс-релизы" + contact footer) → classifier dropped it as `geo0`. With
   `html_list: true` the collector extracts headline links (anchor text
   ≥ 30 chars, same registrable host, non-nav path), then fetches each linked
   page and parses it as a real article (title + body + date + image). A failed
   per-article fetch degrades to a title-only item (`raw.degraded=true`), never
   aborting the source. Result on a live run: KZ Tier-1 went from 1 → ~5
   articles/source; geo1 canonical went from 1 → 7.
2. **`published_at` fallback + estimated flag.** HTML items with no parseable
   date used to be `None` → silently excluded by the daily window. The collector
   now stamps the collection time when no date is found AND sets
   `RawArticle.published_at_estimated=True`. The estimate is used for
   windowing/sorting only — `digest_renderer._article_date_text` renders
   "дата не указана" for estimated dates, never a fabricated timestamp. This
   matters: a fabricated date is "today", which is *later* than a
   previous-day digest, and the QA skill correctly blocks a digest whose
   article dates post-date it. Never show an estimated date as a real one.
3. **Lookback window** (`is_datetime_in_window`, `digest_lookback_days`).
   The digest filter was a strict single calendar day, so sparse KZ/CIS sources
   that don't publish daily almost never landed on the digest date. The window
   is now configurable: `1` = ТЗ-faithful "previous day" (default), higher =
   look back N days. Settings `CRYPTO_MONITOR_DIGEST_LOOKBACK_DAYS`, CLI
   `--lookback-days`, bot `/crypto_set lookback N`, per-chat
   `digest_lookback_days`. The window also extends its END to `now` for a live
   digest (when `day` is within `lookback_days` of today), so this morning's
   fresh scrape (estimated-dated today) lands in the previous-day digest;
   historical rebuilds stay bounded to the digest day.

Also: `normalize_raw_article` now trusts Kazakh-letter detection over a wrong
source `language_hint` (KZ feeds tag everything `ru`), so Kazakh items get `kk`
and reach the translator.

`ardfm` (gov.kz) is a JS SPA, so `html_list` returned 0. Instead of Playwright,
it now uses the **gov.kz GraphQL collector** (`type: gov_kz`,
[collectors/gov_kz.py](crypto_monitor/collectors/gov_kz.py)). gov.kz exposes a
public Apollo GraphQL endpoint at `https://www.gov.kz/graphql`; the `news` query
is filtered per entity via the team.alabs.hcms filter DSL, which encodes the
operator inside the value: `projects: "EQ:<slug>"`. Sort is
`created_date:DESC`. Each item becomes a real article (title, HTML-stripped
body, date, `heropic` image, `/press/news/details/<slug>` URL). This is an
undocumented endpoint — keep `_size` modest and tolerate schema drift (the
collector raises on a GraphQL `errors` payload so `source_status` records it).
Any other gov.kz entity can be added the same way (set `gov_kz_project`).

## Binance News — assessed, intentionally a stub

The customer asked whether Binance News is an open source. Verdict in
[config/sources.example.yml](config/sources.example.yml) (`binance-news`,
`enabled: false`): open, but **not drop-in**. The public Square RSS is behind
Akamai bot-protection (HTTP 202 to server clients); the `bapi` JSON endpoint
returns 200 but nests articles under `data.catalogs[].articles` with only
`title` + epoch-ms `releaseDate` — no body, URL, or image, so the generic
`json_api` collector would collapse every item to one URL and lose dates. It
needs a source-specific collector (build URL from `code`, parse epoch-ms,
fetch body). That is production backlog, and content-wise it is Tier-3 (listing
announcements, low signal for a KZ bank). Do not enable it without writing the
dedicated collector first.

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
