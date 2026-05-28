---
name: crypto-source-validator
description: Use this skill whenever Claude needs to evaluate whether a website, RSS feed, Telegram channel, or X (Twitter) account is a suitable source for the crypto / digital-assets news monitoring system. Triggers include phrases such as 'should we add this source', 'is this website worth monitoring', 'evaluate this RSS feed', 'check if this Telegram channel is relevant', 'оцени этот источник', 'стоит ли подключать'. Use whenever a content manager is considering adding a new source to the catalog and needs an automated initial assessment before manual approval. The skill returns a structured recommendation with score (0-100), pros, cons, and technical notes.
version: 1.0.0
license: Proprietary
---

# crypto-source-validator

## Назначение

Skill оценивает кандидата на добавление в каталог источников: проверяет тематическое соответствие, частоту публикаций, достоверность, технический доступ, уникальность контента. Возвращает рекомендацию (рекомендовать / отклонить), числовую оценку 0–100, плюсы, минусы и технические замечания.

## Когда использовать

- При работе контент-менеджера через админку: «Хочу добавить новый источник, оцените».
- При периодическом ревью каталога источников (отбраковка устаревших).
- При обработке предложений от получателей сводки («рекомендуйте добавить»).

## Контракт

### Вход

```json
{
  "candidate": {
    "url": "https://example.com",
    "type": "website | rss | telegram | x_account",
    "name": "Example Crypto News",
    "language": "en",
    "country_hint": "US",
    "sample_titles": ["Заголовок 1", "Заголовок 2", "..."],
    "sample_content": "Фрагмент типичной публикации",
    "publication_frequency_per_week": 25,
    "rss_url": "https://example.com/rss" 
  }
}
```

Поля `sample_titles`, `sample_content`, `publication_frequency_per_week` опциональны, но без них уверенность снижается.

### Выход

```json
{
  "recommended": true,
  "score": 78,
  "confidence": 0.85,
  "pros": [
    "Высокая релевантность: 100% sample-публикаций по теме крипто.",
    "Стабильная частота — около 25 публикаций в неделю.",
    "Доступен RSS — простая интеграция."
  ],
  "cons": [
    "Контент частично дублирует CoinDesk; уникальность ограничена.",
    "Источник не указывает редакционную политику и состав редакции."
  ],
  "technical_notes": "RSS работает, проверен; HTTPS включён; в RSS title и pubDate присутствуют, но description короткий.",
  "suggested_priority": 3,
  "suggested_topics_focus": ["exchanges", "products"]
}
```

## Критерии оценки

### 1. Тематическая релевантность (вес 30%)

- Все ли sample-публикации относятся к крипто / финрегу / цифровым активам?
- 100% релевантных → score за критерий 30.
- 70–99% релевантных → score 22.
- 30–69% → score 12 (только для специализированных тем).
- < 30% → score 5 (либо отказ).

### 2. Частота публикаций (вес 15%)

- ≥ 7 публикаций в неделю → 15.
- 3–6 публикаций в неделю → 10.
- 1–2 публикации в неделю → 5.
- < 1 публикации в неделю → 0 (рекомендуется отказ — слишком редко для ежедневной сводки).

### 3. Достоверность (вес 25%)

- Официальный регулятор / центральный банк → 25.
- Авторитетное деловое СМИ с указанием редакции → 22.
- Специализированное крипто-СМИ с историей и редакцией → 18.
- Корпоративный блог / PR-канал → 12.
- Анонимный Telegram-канал → 5–8.
- Сообщество / форум → 3.

### 4. Технический доступ (вес 15%)

- Есть RSS + стабильный HTML → 15.
- Только стабильный HTML → 12.
- Только JS-рендеринг → 8 (требует Playwright).
- Через API с лимитами / платная → 6.
- Только через Web scraping с частой ломкой → 3.

### 5. Уникальность (вес 15%)

- Эксклюзивный контент (например, регулятор) → 15.
- В основном уникальный с редкими перепечатками → 12.
- Смешанное: уникальный + перепечатки → 8.
- В основном перепечатки → 3.
- Чистый агрегатор → 0 (рекомендуется отказ — не добавляет ценности).

## Порог рекомендации

- **score ≥ 60** → recommended=true.
- **score 40–59** → recommended=true только если источник заполняет тематический пробел (например, единственный крипто-источник в стране СНГ).
- **score < 40** → recommended=false.

## Алгоритм

1. **Проверь sample_titles и sample_content** на тематическое соответствие. Если 0% — `recommended=false`, score=0.
2. **Оцени по 5 критериям**, начисли соответствующие баллы.
3. **Суммируй** в общий score.
4. **Сформулируй pros/cons** — конкретные, измеримые.
5. **Определи suggested_priority** по геопринадлежности (см. `crypto-news-classifier/references/geo-priorities.md`).
6. **Определи suggested_topics_focus** — какие топики этот источник освещает лучше всего.
7. **Заполни technical_notes** — особенности доступа, потенциальные сложности парсинга.

## Правила

- **Если в sample нет ни одной крипто-публикации** — отказ, даже если источник важен по другим темам.
- **Если канал анонимный** (только в Telegram, нет идентификации) — максимум recommended=false без специальных условий.
- **Если источник дублирует уже подключённый** — рекомендация «не добавлять», в cons указать конкретный конкурент.
- **Если источник местный** (например, узбекистанский) — пониженный порог по уникальности; такие источники важны для покрытия региона.

## Примеры

### Пример 1: качественный регулятор

Вход:
```json
{"candidate": {"url": "https://afsa.aifc.kz", "type": "website", "name": "AFSA", "language": "en", "country_hint": "KZ", "sample_titles": ["AFSA grants crypto license to XYZ", "AFSA publishes new digital asset framework"], "publication_frequency_per_week": 2, "rss_url": null}}
```

Выход:
```json
{
  "recommended": true,
  "score": 78,
  "confidence": 0.9,
  "pros": [
    "Официальный регулятор Казахстана с высшей достоверностью.",
    "Тематически идеальное соответствие — лицензирование и регулирование.",
    "Эксклюзивный контент: первоисточник."
  ],
  "cons": [
    "Низкая частота публикаций — около 2 в неделю.",
    "Нет RSS — потребуется HTML-парсер."
  ],
  "technical_notes": "Сайт стабильный, статический HTML; рекомендуется парсер на BeautifulSoup. Контент в основном на английском, иногда казахский.",
  "suggested_priority": 1,
  "suggested_topics_focus": ["regulation", "licensing"]
}
```

### Пример 2: качественный крипто-источник

Вход:
```json
{"candidate": {"url": "https://coindesk.com", "type": "rss", "name": "CoinDesk", "language": "en", "country_hint": "US", "sample_titles": ["..."], "publication_frequency_per_week": 80, "rss_url": "https://coindesk.com/feed"}}
```

Выход:
```json
{
  "recommended": true,
  "score": 82,
  "confidence": 0.92,
  "pros": [
    "Авторитетное крипто-СМИ с собственной редакцией.",
    "Высокая частота — 80 публикаций в неделю.",
    "Доступен стабильный RSS.",
    "Часто публикует эксклюзивы (например, регуляторные новости США)."
  ],
  "cons": [
    "Англоязычный — потребуется переводчик в пайплайне.",
    "Объём шумных публикаций (прогнозы цен) — потребуется фильтрация."
  ],
  "technical_notes": "RSS активен, поддерживает Atom и RSS 2.0; rate limit отсутствует. Контент содержит HTML-разметку в descriptions.",
  "suggested_priority": 3,
  "suggested_topics_focus": ["regulation", "exchanges", "defi", "stablecoins"]
}
```

### Пример 3: отказ

Вход:
```json
{"candidate": {"url": "https://t.me/anon_crypto_alpha", "type": "telegram", "name": "Anon Crypto Alpha", "language": "ru", "country_hint": null, "sample_titles": ["🚀🚀🚀 ЭТО ВЗЛЕТИТ!", "Инсайд: завтра памп", "Слили адрес кита"], "publication_frequency_per_week": 50}}
```

Выход:
```json
{
  "recommended": false,
  "score": 12,
  "confidence": 0.95,
  "pros": [],
  "cons": [
    "Анонимный источник без указания авторства.",
    "Сенсационный стиль; преобладает инсайдерская спекуляция, не новости.",
    "Содержание не соответствует корпоративному контексту.",
    "Высокий риск дезинформации."
  ],
  "technical_notes": "Технически доступен через Telethon, но содержимое не пройдёт фильтр релевантности.",
  "suggested_priority": null,
  "suggested_topics_focus": []
}
```

## Антипримеры

❌ Рекомендовать источник, у которого ни одна sample-публикация не про крипто, потому что «домен звучит правильно».

❌ Поставить score 80+ анонимному Telegram-каналу за «высокую частоту».

❌ Отказать официальному регулятору только потому, что у него мало публикаций.

## Граничные случаи

- **Sample пуст** → confidence < 0.5, оценка по url и описанию; в cons указать «sample отсутствует, рекомендуется собрать вручную».
- **Источник на казахском языке без рекомендации** → повышенный score за уникальное языковое покрытие.
- **Источник дублирует уже подключённый** → recommended=false с конкретным указанием альтернативы в cons.
- **Telegram-канал регулятора** (например, @nationalbankkz) → подтверждённый источник; high score.

## Версионирование

- **1.0.0** — первая версия, 5 критериев, шкала 0–100.
