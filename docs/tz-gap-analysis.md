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
| ФТ-4 Classification | Реализовано | Через `crypto-news-classifier`. Тег `events` и поля `is_legislative`/`event_*` добавлены в v1.1. |
| ФТ-5 Summarization | Реализовано | Через `crypto-news-summarizer`, с QA skill. |
| ФТ-6 Приоритизация и сортировка | Реализовано для MVP | Сортировка: geo priority, priority, score, chronology. Авто-эскалация для законодательства РК/СНГ, авто-оценка для мероприятий по `event_scale`, анти-усиление для агрегаторных перепечаток мелких US/EU enforcement (v1.1). Квоты KZ/CIS/legislation enforced в коде (`_enforce_quotas`). |
| ФТ-7.1 Сводка за предыдущие сутки | Реализовано | CLI/Telegram используют date-filter по локальному дню. |
| ФТ-7.2 Разделы сводки | Реализовано | 9 секций в local renderer и digest skill: Законодательство (защищён), Регулирование РК, Регулирование СНГ, CBDC, Банки, Биржи, Технологии, Международные, Мероприятия. |
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
- **Покрытие KZ/CIS контентом** (после фидбэка о доминировании US-новостей):
  расширен каталог источников до 17 (KZ-регулятор переключён на работающий
  HTML-эндпойнт; добавлены AFSA, AIFC, ARDFM, Astana Hub, KASE, Kapital.kz,
  Kursiv, Forbes.kz, ForkLog, Incrypted, CoinSpot, DeCenter, Habr Crypto,
  Bank of Russia RSS); CoinDesk/Cointelegraph понижены до priority=3 с
  poll_interval=90 мин.
- **Защита KZ/CIS-секций при ранжировании**: Python-квоты 20% KZ / 20% CIS /
  10% legislation в `apply_ranking_response` гарантируют, что глобальные
  новости не вытесняют локальный контент даже при пустом ответе от Gemini.
- **Раздел «Законодательные изменения»** в дайджесте (новый Раздел 0):
  защищён от усечения по `total_max_items`, сортируется по стадии
  (`signed > adopted > debated > introduced > in_force`).
- **Раздел «Мероприятия и форумы»** в дайджесте (новый Раздел 8): только
  `event_scale=kz_major | cis_major | global_major`; в карточке выводятся
  `event_date` и `event_location`.
- **Анти-усиление для иностранных enforcement-перепечаток**: в `RANKING_TASK`
  и `crypto-news-prioritizer/references/aggregator-foreign-enforcement.md`
  зафиксированы условия, при которых действия US/EU регулятора любого
  типа (иски, штрафы, санкции OFAC, заморозки) против непубличных компаний
  или площадок третьих стран (Иран, Северная Корея и т. п.) получают
  low (см. Privvy- и Nobitex-кейсы из фидбэка).
- **Quality floor**: статьи с `priority: low` отфильтровываются на уровне
  пайплайна (`_enforce_quotas`, `build_digest`) и рендерера
  (`render_digest_locally`). На тихие дни вместо шумной сводки выводится
  явное «Сегодня не зафиксировано значимых событий…». Тишина строго лучше
  шума: показ low-сигнала противоречит собственному вердикту
  прайоритайзера.

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
