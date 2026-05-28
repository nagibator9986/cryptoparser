---
name: crypto-digest-quality-check
description: Use this skill whenever Claude needs to QA-check a daily crypto news digest before it is sent to recipients. Triggers include phrases such as 'check the digest', 'validate the summary', 'is this digest ready to send', 'QA this brief', 'проверь сводку', 'готова ли сводка к отправке'. Use as a quality gate AFTER the digest is assembled and BEFORE delivery. The skill checks for copyright issues (verbatim quotes > 15 words), factual sanity (impossible dates / figures), tonal issues (sensationalism, emotional language), structural completeness (all required sections present), adequate prioritization (KZ items in section 1 when available), and format integrity (Markdown escaping, HTML validity hints).
version: 1.0.0
license: Proprietary
---

# crypto-digest-quality-check

## Назначение

Skill — финальный контроль качества сводки перед отправкой. Принимает на вход собранную сводку (HTML, plain text, Telegram-сегменты, метаданные) и возвращает вердикт `passed` / `failed` с детальным списком замечаний.

## Когда использовать

- Сразу после `crypto-digest-builder` и перед `delivery`.
- При ручном пересмотре сводки перед массовой рассылкой.
- При расследовании жалоб получателей.

## Контракт

### Вход

```json
{
  "digest_date": "2025-03-12",
  "html": "<html>...</html>",
  "plain_text": "...",
  "telegram_segments": ["..."],
  "articles": [
    {"id": "...", "title_ru": "...", "summary": "...", "source_url": "...", "topics": [...], "country": "...", "geo_priority": 1, "priority": "high"}
  ],
  "stats": {...}
}
```

### Выход

```json
{
  "passed": false,
  "severity": "blocker",
  "issues": [
    {
      "category": "copyright",
      "severity": "blocker",
      "article_id": "art_007",
      "description": "Реферат содержит дословную цитату из источника длиной 22 слова.",
      "evidence": "...фрагмент..."
    }
  ],
  "warnings": [...],
  "recommendation": "do_not_send"
}
```

Поля:
- **passed** — true, если нет проблем уровня blocker.
- **severity** — максимальная серьёзность найденных проблем: `blocker` / `major` / `minor` / `none`.
- **issues** — массив проблем; каждая с категорией, серьёзностью, ссылкой на статью.
- **warnings** — нестрогие замечания (не блокируют отправку).
- **recommendation** — одно из: `send`, `send_with_caution`, `do_not_send`.

## Категории проверок

### 1. Copyright (категория `copyright`)

- **blocker**: дословная фраза из source длиннее 15 слов.
- **major**: дословная фраза из source длиной 10–15 слов без атрибуции.
- **minor**: структура реферата близко повторяет структуру оригинала.

### 2. Factual sanity (категория `factual`)

- **blocker**: невозможная дата (например, 30 февраля), цифра с очевидной ошибкой (например, штраф «$1 миллиард миллиардов»).
- **major**: упомянуто несуществующее учреждение или неправильное имя регулятора (например, «Министерство финансов SEC»).
- **minor**: округление или несовпадение цифры с источником в пределах 10%.

### 3. Tonal (категория `tone`)

- **blocker**: ненормативная лексика или прямые оскорбления.
- **major**: сенсационные обороты («шокирующий», «эпохальный»), эмоциональные оценки.
- **minor**: разговорный стиль, излишняя дружелюбность.

### 4. Structural (категория `structure`)

- **blocker**: отсутствует один из обязательных элементов (дата, статистика, подвал).
- **major**: дубликаты публикаций в одной сводке.
- **minor**: пустые разделы со заголовком, но без публикаций.

### 5. Prioritization (категория `priority`)

- **major**: при наличии geo_priority=1 публикаций раздел «Регулирование РК» отсутствует.
- **minor**: статья с priority=critical не находится в верхней части своего раздела.

### 6. Format (категория `format`)

- **blocker**: невалидный HTML (незакрытые теги).
- **blocker**: незаэкранированный спецсимвол MarkdownV2 в Telegram-сегменте.
- **major**: длина telegram-сегмента превышает 4000 символов.
- **minor**: отсутствие активной ссылки на оригинал у одной из публикаций.

### 7. Length (категория `length`)

- **major**: summary < 50 слов или > 150 слов.
- **minor**: summary 50–60 слов или 120–150 слов (на границе нормы).

## Справочники

- `references/checklist.md` — полный детализированный чек-лист.

## Алгоритм

1. **Пройди по каждой публикации**, проверяя категории 1, 2, 3, 7.
2. **Проверь сводку в целом**, категории 4, 5, 6.
3. **Сформируй список issues и warnings.**
4. **Определи severity** — максимальная среди issues.
5. **Сформулируй recommendation:**
   - Если есть blocker → `do_not_send`.
   - Если есть major (но нет blocker) → `send_with_caution`.
   - Иначе → `send`.

## Правила

- **Один issue = одно замечание.** Не объединяй разные проблемы.
- **Указывай article_id, когда замечание относится к конкретной публикации.**
- **Evidence — конкретный фрагмент текста**, чтобы оператор мог быстро найти проблему.
- **Не предлагай конкретные исправления** в этом skill — это работа отдельного редактора.

## Примеры

### Пример 1: passed

Вход — корректно собранная сводка из 3 публикаций.

Выход:
```json
{
  "passed": true,
  "severity": "none",
  "issues": [],
  "warnings": [],
  "recommendation": "send"
}
```

### Пример 2: blocker по копирайту

Вход — сводка, в которой реферат содержит точную цитату из оригинала длиной 18 слов.

Выход:
```json
{
  "passed": false,
  "severity": "blocker",
  "issues": [
    {
      "category": "copyright",
      "severity": "blocker",
      "article_id": "art_007",
      "description": "Реферат содержит дословную цитату из источника длиной 18 слов без атрибуции.",
      "evidence": "...The U.S. Securities and Exchange Commission today announced charges against crypto asset trading platform Bittrex..."
    }
  ],
  "warnings": [],
  "recommendation": "do_not_send"
}
```

### Пример 3: warnings без блокеров

Вход — корректная сводка, но один реферат — 145 слов (длинноват).

Выход:
```json
{
  "passed": true,
  "severity": "minor",
  "issues": [],
  "warnings": [
    {
      "category": "length",
      "severity": "minor",
      "article_id": "art_003",
      "description": "Реферат содержит 145 слов, превышая рекомендованный диапазон 60–120."
    }
  ],
  "recommendation": "send"
}
```

## Антипримеры

❌ Раздуть один blocker до нескольких issues, повторяя одно и то же.

❌ Помечать как issue стилистическое расхождение, не относящееся к чек-листу.

❌ Не указывать article_id, когда замечание точно к одной статье.

## Граничные случаи

- **Очень короткая сводка (1 публикация)** → не проверяй prioritization и structural жёстко; считай малое наполнение нормой.
- **Telegram-сегмент отсутствует** (только email) → не проверяй format для Telegram.
- **Спорный случай tone** → ставь severity `minor` и `warnings`, не блокируй.

## Версионирование

- **1.0.0** — первая версия, 7 категорий проверок.
