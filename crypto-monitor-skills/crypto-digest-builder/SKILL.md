---
name: crypto-digest-builder
description: Use this skill whenever Claude needs to assemble a daily crypto news digest from a set of pre-processed (classified, summarized, prioritized) articles. Triggers include phrases such as 'build the digest', 'assemble the morning brief', 'create the email summary', 'render the Telegram digest', 'собери сводку', 'сформируй дайджест'. Use whenever the user has a structured list of articles and needs a polished, sectioned, dated digest in HTML / plain text / Telegram MarkdownV2 format ready for delivery to corporate recipients. The skill enforces Kazakhstan-first ordering and produces three output formats simultaneously.
version: 1.0.0
license: Proprietary
---

# crypto-digest-builder

## Назначение

Skill принимает массив обработанных публикаций (с готовыми рефератами, классификацией, приоритетами) и формирует финальную ежедневную сводку в трёх параллельных форматах: HTML для email, plain text для fallback, MarkdownV2 для Telegram.

## Когда использовать

- Ежедневно в 08:30 (Asia/Almaty) после завершения этапов обработки.
- При ручном запросе пересборки за прошлый день.
- При генерации тестовых сводок для проверки шаблонов.

## Контракт

### Вход

```json
{
  "digest_date": "2025-03-12",
  "articles": [
    {
      "id": "art_001",
      "title_ru": "...",
      "summary": "...",
      "topics": ["cbdc", "regulation"],
      "country": "KZ",
      "geo_priority": 1,
      "priority": "high",
      "score": 76,
      "source_name": "nationalbank.kz",
      "source_url": "https://...",
      "published_at": "2025-03-12T08:00:00+05:00"
    }
  ],
  "max_items_per_section": 5,
  "total_max_items": 25
}
```

### Выход

```json
{
  "digest_date": "2025-03-12",
  "html": "<html>...</html>",
  "plain_text": "...",
  "telegram_segments": ["сегмент 1...", "сегмент 2..."],
  "stats": {
    "total_articles": 22,
    "by_section": {"regulation_kz": 3, "regulation_cis": 2, "cbdc": 4, "banks": 3, "exchanges": 5, "tech": 2, "international": 3},
    "skipped_due_to_limit": 5
  }
}
```

## Структура сводки (по разделам)

1. **Шапка.** Дата сводки, период покрытия, общая статистика (X новостей).
2. **Раздел 1. Регулирование Республики Казахстан** — geo_priority=1, topics включают regulation/licensing.
3. **Раздел 2. Регулирование СНГ и Центральной Азии** — geo_priority=2, topics включают regulation/licensing.
4. **Раздел 3. CBDC и государственные цифровые инициативы** — topics включает cbdc.
5. **Раздел 4. Банки и финтех** — topics включает banks; geo_priority 1–2 в приоритете.
6. **Раздел 5. Биржи, продукты, лицензирование** — topics: exchanges, products, licensing (которые не попали в разделы 1–2).
7. **Раздел 6. Технологии, инфраструктура, безопасность** — topics: blockchain-platforms, wallets, security-incidents, ai-in-crypto, tokenization, defi, stablecoins.
8. **Раздел 7. Кратко: международные новости** — geo_priority=3 публикации, не вошедшие в специализированные разделы. Только priority=high/critical.
9. **Подвал.** Ссылки на архив, на управление подпиской, дисклеймер.

### Правила распределения

- **Одна публикация — один раздел.** Если статья подходит к нескольким, выбирай по наиболее значимому тегу (например, новость о банке, выпустившем стейблкоин, идёт в «Банки и финтех», не в «Стейблкоины»).
- **Внутри раздела сортируй** по `score` убыванию.
- **Ограничение объёма раздела** — `max_items_per_section` (по умолчанию 5).
- **Если раздел пуст** — не показывай заголовок раздела.

## Алгоритм

1. **Сгруппируй** статьи по разделам по правилам выше.
2. **Отсортируй** внутри каждого раздела по score.
3. **Усеки** разделы до `max_items_per_section`.
4. **Проверь общий лимит** `total_max_items`. Если суммарно больше — отрезай с раздела «Кратко: международные новости», затем «Технологии», постепенно сужаясь.
5. **Сгенерируй HTML** по шаблону `assets/email-template.html`.
6. **Сгенерируй plain text** для email-fallback.
7. **Сгенерируй Telegram-сегменты** по шаблону `assets/telegram-template.md`:
   - Один сегмент ≤ 4000 символов (лимит Telegram — 4096).
   - Не разрывай блоки одной публикации между сегментами.
   - В конце каждого сегмента, кроме последнего, — пометка «(продолжение далее...)».

## Правила оформления

### Структура блока одной публикации

```
[ПРИОРИТЕТ-МАРКЕР] Заголовок (русский)
Реферат (60–120 слов)
Источник: <название>  |  Дата: <DD.MM>  |  [Читать оригинал →]
```

Приоритет-маркеры:
- 🔴 critical
- 🟠 high
- 🟡 medium
- ⚪ low (обычно не выводится в сводку)

### Стиль

- **Шапка раздела:** русский, без эмодзи в HTML, с эмодзи в Telegram.
- **Заголовки публикаций:** жирным, без точки в конце.
- **Рефераты:** прямой шрифт, выровнен по ширине в HTML.
- **Ссылки:** «Читать оригинал →» (HTML), `[Источник](URL)` (Markdown).
- **Дисклеймер в подвале:** «Сводка сформирована автоматически. Возможны неточности в кратком изложении; для принятия решений обращайтесь к оригиналу. Для отписки: [ссылка].»

## Справочники и шаблоны

- `assets/email-template.html` — HTML-шаблон с inline-стилями (Outlook-совместимый).
- `assets/telegram-template.md` — шаблон Telegram MarkdownV2 с экранированием.
- `references/style-guide.md` — стиль изложения сводки.

## Примеры

### Пример 1: один блок публикации (HTML)

```html
<div style="border-left: 4px solid #1F4E79; padding: 12px 16px; margin: 16px 0; background: #F5F9FD;">
  <div style="color: #ED6C02; font-size: 11px; font-weight: bold;">🟠 HIGH PRIORITY</div>
  <h3 style="font-family: Arial, sans-serif; font-size: 16px; color: #1F4E79; margin: 4px 0;">
    Запуск второй фазы пилота цифрового тенге с участием четырёх банков
  </h3>
  <p style="font-family: Arial, sans-serif; font-size: 14px; color: #1F1F1F; line-height: 1.5;">
    Национальный банк РК объявил о начале второй фазы пилотного проекта цифрового тенге. К проекту подключились четыре системообразующих банка — Halyk, Kaspi, Forte и Jusan...
  </p>
  <div style="font-family: Arial, sans-serif; font-size: 12px; color: #666; margin-top: 8px;">
    Источник: nationalbank.kz | Дата: 12.03 | <a href="https://nationalbank.kz/..." style="color: #2E75B6;">Читать оригинал →</a>
  </div>
</div>
```

### Пример 2: один блок (Telegram MarkdownV2)

```
🟠 *HIGH*
*Запуск второй фазы пилота цифрового тенге с участием четырёх банков*

Национальный банк РК объявил о начале второй фазы пилотного проекта цифрового тенге\. К проекту подключились четыре системообразующих банка — Halyk, Kaspi, Forte и Jusan\.\.\.

📅 12\.03 \| 📰 nationalbank\.kz \| [Читать оригинал](https://nationalbank.kz/...)

\-\-\-
```

Обрати внимание на экранирование точек и дефисов согласно MarkdownV2.

## Антипримеры

❌ Сводка без раздела «Регулирование РК» при наличии 3 публикаций с geo_priority=1 и тегом regulation.

❌ Telegram-сегмент, разрывающий блок публикации пополам.

❌ HTML без inline-стилей (Outlook не поддерживает внешние CSS).

❌ Сводка без даты в шапке.

## Граничные случаи

- **Нет публикаций по разделу** → раздел не выводится, не заменяется заглушкой.
- **Все публикации одного раздела** → остальные разделы отсутствуют, в шапке отметка «Сегодня основная тематика — <X>».
- **Слишком короткая сводка** (< 3 публикаций) → добавь в шапку «Малое количество публикаций за период».
- **Telegram-сегментов получилось >6** → пересмотри `total_max_items`; обычно сводка должна укладываться в 2–4 сегмента.

## Версионирование

- **1.0.0** — первая версия. 7 разделов; 4-уровневая приоритезация; три формата на выходе.
