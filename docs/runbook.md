# Runbook

## Назначение

Runbook описывает эксплуатацию локального/MVP варианта Crypto Monitor:
проверки, сбор, обработку, доставку, Telegram-бота и базовую диагностику.

## Быстрая проверка

```bash
python3 -m pytest -q
python3 -m crypto_monitor.cli status
python3 -m crypto_monitor.cli evals --dry-run
python3 -m crypto_monitor.cli run --input-json sample_articles.json --dry-run
```

## Конфигурация

Основные переменные:

- `GEMINI_API_KEY` - ключ Gemini для реального LLM-run.
- `GEMINI_MODEL` - модель, например `gemini-2.5-flash`.
- `CRYPTO_MONITOR_DB_PATH` - SQLite-файл.
- `CRYPTO_MONITOR_SOURCES_FILE` - YAML-каталог источников.
- `SMTP_*` - email-доставка.
- `TELEGRAM_BOT_TOKEN` - Telegram command/delivery bot.
- `TELEGRAM_CHAT_ID` - optional fallback chat for CLI `digest --telegram`.

## Daily Flow

Ручной MVP-flow:

```bash
crypto-monitor collect
crypto-monitor process
crypto-monitor digest --date YYYY-MM-DD --telegram
```

Если `--date` не указан, система берет предыдущий локальный день
`Asia/Almaty`, что соответствует ТЗ: сводка за прошедшие сутки.

## Telegram Bot

Запуск:

```bash
crypto-monitor telegram-bot
```

Первичная настройка в группе:

```text
/crypto_start
/crypto_set delivery on
/crypto_set timezone Asia/Almaty
/crypto_schedule 09:00 weekdays
/crypto_sources
```

Полезные команды:

```text
/crypto_collect
/crypto_process
/crypto_digest YYYY-MM-DD
/crypto_latest
/crypto_run YYYY-MM-DD
/crypto_search AFSA
```

Плановая отправка выполняется в окно `digest_time ± 5 минут`.
По умолчанию `digest_time=09:00`, `weekdays=daily`, `delivery=off`.

Расписание можно менять прямо из группы:

```text
/crypto_schedule
/crypto_schedule 09:00 daily
/crypto_schedule 09:00 weekdays
/crypto_schedule 09:00 пн-пт
/crypto_schedule 11:30 weekends
/crypto_schedule 10:15 пн,ср,пт
/crypto_set weekdays будни
/crypto_set digest_time 08:45
```

Поддерживаются алиасы: `daily`, `weekdays`, `weekends`, `mon,tue,wed`,
`mon-fri`, `пн,ср,пт`, `пн-пт`, `будни`, `выходные`.

## AI Ranking

Перед сборкой сводки выполняется отдельный cross-article ranking pass через
Gemini skill `crypto-news-prioritizer`. Это не простая сортировка по старым
score: модель получает весь набор кандидатов и ранжирует его для аудитории
банка в РК. Локальный fallback сохраняет порядок: геоприоритет, priority,
score, дата публикации.

## Diagnostics

Проверить состояние БД:

```bash
crypto-monitor status
```

Проверить здоровье источников:

```bash
crypto-monitor sources-status
```

Найти публикации или сводки:

```bash
crypto-monitor search "цифровой тенге"
crypto-monitor search AFSA --kind processed --country KZ
```

Проверить audit log:

```bash
crypto-monitor audit --limit 100
```

## Incident Playbook

Если нет свежей сводки:

1. Проверить `crypto-monitor audit`.
2. Проверить `crypto-monitor sources-status`.
3. Выполнить `crypto-monitor process --dry-run`.
4. Собрать сводку вручную: `crypto-monitor digest --date YYYY-MM-DD --dry-run`.
5. Проверить `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, SMTP settings.

Если источник падает:

1. Посмотреть `last_error` в `sources-status`.
2. Проверить URL из `config/sources.example.yml`.
3. Временно отключить источник в YAML.
4. Для production вынести источник в отдельный parser plugin.

Если LLM недоступен:

1. Проверить `GEMINI_API_KEY`.
2. Запустить dry-run для проверки остального pipeline.
3. Использовать локальный fallback renderer для уже обработанных статей.

## Backup

Для MVP достаточно регулярного копирования SQLite-файла и каталога конфигов:

```bash
cp data/crypto_monitor.sqlite3 "backup/crypto_monitor-$(date +%F).sqlite3"
```

Для production ТЗ требует PostgreSQL, зашифрованные backups и регулярную
проверку восстановления.
