# Crypto Monitor Gemini

Профессиональная MVP-реализация системы мониторинга цифровых активов на базе уже созданных `crypto-monitor-skills`, но с AI-провайдером **Google Gemini**.

## Что входит

- RSS/HTML/JSON API collectors для первичного сбора публикаций.
- Нормализация в `RawArticle` / `ProcessedArticle`.
- AI-пайплайн через Gemini:
  - перевод;
  - классификация;
  - дедупликация;
  - реферирование;
  - приоритизация;
  - сборка дайджеста;
  - QA;
  - оценка новых источников.
- SQLite-хранилище для local/MVP.
- Email и Telegram delivery adapters.
- Telegram command bot для настройки группы, ручного запуска pipeline и плановой отправки.
- CLI `crypto-monitor`.
- Dockerfile и docker-compose.
- Автоматический runner для `crypto-monitor-skills/*/evals/evals.json`.
- Санитайзинг untrusted-контента перед отправкой в Gemini.

## Быстрый старт

```bash
cd /Users/a1111/Desktop/projects/Diplomas/Diploma\ projects/ccu/cryptoparser
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Укажите ключ:

```bash
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

Проверка без Gemini API:

```bash
crypto-monitor skills
crypto-monitor run --input-json sample_articles.json --dry-run
crypto-monitor evals --dry-run
crypto-monitor status
crypto-monitor sources-status
crypto-monitor search AFSA --kind processed
crypto-monitor digests
crypto-monitor audit
```

Полный запуск с Gemini:

```bash
crypto-monitor run --input-json sample_articles.json
```

Проверка качества skills через Gemini:

```bash
crypto-monitor evals
crypto-monitor evals --skill crypto-news-classifier
```

Сбор из источников:

```bash
# Включите нужные sources в config/sources.example.yml
crypto-monitor collect
crypto-monitor process
crypto-monitor digest --date 2026-05-24
crypto-monitor show-digest dry-run --format plain
```

Если `--date` не указан для сборки сводки, используется предыдущий локальный
день `Asia/Almaty`, как в ТЗ для ежедневной утренней сводки.

## Telegram-бот в группе

Для работы командного бота нужен `TELEGRAM_BOT_TOKEN`. `TELEGRAM_CHAT_ID` больше не обязателен
для группового режима: администратор может зарегистрировать чат командой прямо в Telegram.

```bash
crypto-monitor telegram-bot
```

Полезные команды в группе:

```text
/crypto_start
/crypto_settings
/crypto_schedule
/crypto_schedule 09:00 weekdays
/crypto_schedule 09:00 пн-пт
/crypto_schedule 10:30 пн,ср,пт
/crypto_set timezone Asia/Almaty
/crypto_set digest_time 09:00
/crypto_set weekdays weekends
/crypto_set delivery on
/crypto_set dry_run on
/crypto_set min_priority high
/crypto_set auto_collect off
/crypto_set auto_process off
/crypto_sources
/crypto_sources coindesk,afsa-aifc
/crypto_collect
/crypto_process
/crypto_digest 2026-05-24
/crypto_latest
/crypto_run 2026-05-24
/crypto_search AFSA
```

Команды изменения настроек и запуска pipeline доступны только администраторам группы.
Настройки сохраняются в SQLite отдельно для каждого чата:

- `delivery` - включает плановую отправку;
- `timezone`, `digest_time`, `weekdays` - локальное расписание отправки;
- `digest_limit`, `section_limit`, `total_limit` - объем сводки;
- `min_priority` - минимальный приоритет публикаций;
- `dry_run` - безопасный режим без Gemini API;
- `previews` - предпросмотр ссылок в Telegram;
- `sources` - выбор источников из `CRYPTO_MONITOR_SOURCES_FILE`;
- `auto_collect` и `auto_process` - автоматический сбор и обработка перед плановой сводкой.

Проверить сохраненные чаты можно из CLI:

```bash
crypto-monitor telegram-chats
```

Поддерживаемые значения дней: `daily`, `weekdays`, `weekends`, `mon,tue,wed`,
`mon-fri`, `пн,ср,пт`, `пн-пт`, `выходные`, `будни`.

## Railway deploy

Проект подготовлен для Railway: `Dockerfile` запускает `crypto-monitor railway`,
`railway.toml` настраивает `/health`, а SQLite рассчитан на volume `/data`.
Пошаговая инструкция: [docs/railway.md](docs/railway.md).

## AI ranking

Перед сборкой сводки pipeline делает отдельный Gemini ranking pass по всему
набору кандидатов. Модель сравнивает публикации между собой в контексте банка
в Казахстане: выше ставятся регулирование РК, AFSA/AIFC/НБ РК, лицензии, CBDC,
банковские продукты, крупные инциденты безопасности и инфраструктурные события.
Ценовые прогнозы, промо и мелкие token updates понижаются или отбрасываются.

## Документация

- [Анализ соответствия ТЗ](docs/tz-gap-analysis.md)
- [Runbook эксплуатации](docs/runbook.md)

## Конфигурация Gemini

Проект использует официальный Google Gen AI SDK:

```python
from google import genai

client = genai.Client(api_key=GEMINI_API_KEY)
response = client.models.generate_content(...)
```

`crypto_monitor/gemini.py` дополнительно включает JSON mode через `response_mime_type="application/json"` и системную инструкцию из выбранного `SKILL.md`.

## Архитектура

```text
collectors -> raw_articles -> Gemini skills -> processed_articles
                                    |
                                    v
                            digest builder -> QA -> delivery
```

Skills остаются внешними prompt-модулями в `crypto-monitor-skills/`. Код их не дублирует: `SkillLoader` читает `SKILL.md`, `references/` и при необходимости `assets/`.

## Production notes

Для промышленной версии по ТЗ следует заменить/расширить:

- SQLite -> PostgreSQL 15+.
- Локальный sequential runner -> Celery/RQ workers + Redis/RabbitMQ.
- Generic HTML collector -> source-specific parser plugins.
- Telegram/X collectors -> MTProto/X API connectors.
- `.env` secrets -> Vault/Secret Manager.
- CLI orchestration -> scheduler at 08:30 Asia/Almaty.
- Delivery command -> managed retry queue and audit log.

Текущая реализация уже отделяет эти слои. Для MVP добавлены retries доставки и SQLite audit-log;
для промышленной версии их нужно перенести в управляемую очередь и неизменяемое хранилище аудита.

## Security notes

Перед вызовом Gemini текст из внешних источников проходит через `sanitize_untrusted_text`.
Он удаляет типовые prompt-injection фразы вроде "ignore previous instructions",
ограничивает длину текста и сохраняет предупреждения в `ProcessedArticle.warnings`.
Оригинальный текст при этом остаётся в `original_body` для аудита и QA-контроля копирайта.
