# Анализ соответствия ТЗ

Документ основан на `ТЗ_Мониторинг_цифровых_активов.docx`.
Текущее состояние проекта: локальный Python MVP с Gemini-адаптером, каталогом
`crypto-monitor-skills`, SQLite-хранилищем, CLI, email/Telegram delivery и
Telegram command bot.

## Итог

Проект закрывает ядро MVP: сбор RSS/HTML/JSON, нормализацию, LLM-pipeline,
skills/evals, дедупликацию, реферирование, приоритизацию, сборку сводки,
архивирование, email/Telegram delivery и базовое администрирование Telegram-группы.

Проект не является полной production-реализацией ТЗ: PostgreSQL, Celery/Redis,
Prometheus/Grafana, SSO/LDAP, Vault, Telegram/X ingestion, полнотекстовый индекс
уровня PostgreSQL/OpenSearch и workflow ручной модерации остаются production backlog.
Веб-админка намеренно не добавлялась: управление MVP перенесено в команды
Telegram-группы.

## Матрица требований

| Требование ТЗ | Статус | Комментарий |
| --- | --- | --- |
| ФТ-1.1 RSS/HTML/JSON/Telegram/X | Частично | RSS, static HTML, JSON API есть. Telegram/X ingestion не реализован. |
| ФТ-1.2 Индивидуальное расписание источников | Частично | `poll_interval_minutes` есть в конфиге; отдельного scheduler по источникам нет. |
| ФТ-1.3 Сырой ответ источника | Частично | В payload сохраняется excerpt/metadata ответа, не отдельный object store. |
| ФТ-1.4 Алерт при недоступности >2 часов | Частично | Добавлен статус источников и ошибки; внешнего мониторинга/alertmanager нет. |
| ФТ-1.5 Изоляция парсеров | Реализовано | Ошибка одного источника логируется и не ломает сбор остальных. |
| ФТ-2.1 Нормализация полей | Реализовано | title/body/date/url/source/language сохраняются. |
| ФТ-2.2 Asia/Almaty timezone | Реализовано для MVP | Даты нормализуются в `Asia/Almaty`. |
| ФТ-2.3 Очистка текста | Реализовано для MVP | BeautifulSoup + JSON-LD + Open Graph; динамические страницы остаются на Playwright для прод. |
| ФТ-2.4 Автоопределение языка | Реализовано для MVP | Добавлен эвристический ru/en/kk detector. |
| ФТ-3.1 URL-дубли | Реализовано | Добавлен exact URL dedup с очисткой tracking-параметров. |
| ФТ-3.2 Semantic dedup | Частично | LLM-skill кластеризует; embeddings/MinHash нет. |
| ФТ-3.3 Кластеры событий | Частично | Skill возвращает canonical IDs; хранение links-to-duplicates не выделено в схему. |
| ФТ-4 Classification | Реализовано | Через `crypto-news-classifier`. |
| ФТ-5 Summarization | Реализовано | Через `crypto-news-summarizer`, с QA skill. |
| ФТ-6 Приоритизация и сортировка | Реализовано для MVP | Сортировка: geo priority, priority, score, chronology. |
| ФТ-7.1 Сводка за предыдущие сутки | Реализовано | CLI/Telegram используют date-filter по локальному дню. |
| ФТ-7.2 Разделы сводки | Реализовано | 7 секций есть в local renderer и digest skill. |
| ФТ-7.3 Дата в блоке | Реализовано | Дата публикации выводится в HTML/plain/Telegram fallback. |
| ФТ-7.4 Доставка 09:00 Алматы | Частично | Telegram bot отправляет в 09:00 ±5 минут; системного scheduler для email нет. |
| ФТ-7.5 Модерация | Не реализовано | Нужен отдельный approval workflow. |
| ФТ-7.6 Email HTML/plain | Реализовано | SMTP с HTML alternative и plain fallback. |
| ФТ-7.7 Telegram MarkdownV2 | Реализовано | Per-article sendPhoto с MarkdownV2-caption, секционные заголовки, fallback на текст при ошибке фото. |
| ФТ-7.8 Retry delivery | Реализовано | 3 попытки с exponential backoff. |
| ФТ-8 Архив и поиск | Частично | Архив SQLite и CLI/Telegram search есть; FTS-индекса нет. |
| ФТ-9 Управление | Частично | Telegram group settings есть; управление выполняется командами в группе. |
| NFR Docker | Реализовано для MVP | Dockerfile и docker-compose есть. |
| NFR Audit log | Частично | SQLite audit log есть; неизменяемое 2-летнее хранилище нет. |
| NFR SSO/Vault/Prometheus | Не реализовано | Требует production-инфраструктуры. |
| Skills/evals >=80% | Реализовано в dry-run | `crypto-monitor evals --dry-run` показывает 100%. |

## Улучшения, внесенные после сверки с ТЗ

- Нормализация дат в `Asia/Almaty`.
- Эвристическое определение языка `ru/en/kk`.
- Date-filter для ежедневной сводки за локальные сутки.
- Default digest date: предыдущий день, как в ТЗ.
- Telegram delivery window: `09:00 ± 5 минут`.
- Telegram chat schedule: time + weekdays, configured directly in the group.
- Сортировка сводки по геоприоритету, значимости, score и хронологии.
- Cross-article Gemini ranking перед сборкой дайджеста (rubric с tier-классификацией источников и `has_image`).
- Дата публикации в блоках HTML/plain/Telegram.
- Exact URL dedup fallback, устойчивый к `utm_*`, `fbclid`, `gclid`.
- Статусы источников: last success/error, consecutive failures, article count.
- CLI `sources-status`.
- CLI/Telegram search по архиву.
- Дополнительные unit-тесты на normalization, storage, digest renderer, Telegram bot.
- **feedparser** для RSS: enclosures, media:content/thumbnail, itunes:image, content:encoded `<img>`.
- **BeautifulSoup + JSON-LD** для HTML: og:image / twitter:image / link[rel=image_src], NewsArticle structured data.
- **Validated image flow**: `normalize_image_url` отсекает data: URIs и tracking pixels, резолвит relative paths.
- **Per-article Telegram sendPhoto** с MarkdownV2-caption, секционные заголовки и fallback на текст при ошибке доставки фото.
- **Inline-keyboard UI**: главное меню, настройки, расписание, источники, приоритет, действия (с RBAC).

## Production backlog

1. PostgreSQL schema: publications, sources, recipients, digests, audit, source_status.
2. Celery/RQ + Redis for ingestion/AI/delivery queues and DLQ.
3. Расширенные Telegram-команды управления с RBAC, аудитом изменений и recipient/source presets.
4. Telegram/X ingestion через MTProto/API.
5. Source-specific HTML parsers and optional Playwright for dynamic pages.
6. Full-text search via PostgreSQL FTS or OpenSearch.
7. Manual moderation workflow: draft -> approve -> send.
8. Prometheus metrics, Grafana dashboards and alertmanager integration.
9. Secrets in Vault/Secret Manager instead of plain `.env`.
10. Backup/restore scripts and quarterly restore rehearsal.
