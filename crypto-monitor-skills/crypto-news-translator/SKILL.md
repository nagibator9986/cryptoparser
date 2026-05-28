---
name: crypto-news-translator
description: Use this skill whenever Claude needs to translate a crypto / fintech / financial-regulatory news article into Russian. Triggers include phrases such as 'translate this', 'переведи на русский', 'localize this article', 'render in Russian'. Trigger for any non-Russian input where the output must be in Russian and the terminology must follow industry conventions (e.g., 'stablecoin' → 'стейблкоин', 'CBDC' остаётся 'CBDC', 'Securities and Exchange Commission' → 'Комиссия по ценным бумагам и биржам США (SEC)'). Use ALSO when the user asks to localize, render, or convert any crypto / digital-assets article to Russian for a Kazakh / CIS audience.
version: 1.0.0
license: Proprietary
---

# crypto-news-translator

## Назначение

Skill переводит публикации о цифровых активах с любого языка на русский язык, сохраняя профессиональную терминологию криптоиндустрии, официальные русские названия международных организаций и регуляторов, и нейтральный новостной регистр.

## Когда использовать

- Перед этапом summarizer для иноязычных публикаций.
- Когда нужно подготовить полный перевод (не реферат) для архива.
- Когда контент будет использован в коммуникации, требующей точности (внутренние отчёты).

## Контракт

### Вход

```json
{
  "title": "Заголовок (любой язык)",
  "body": "Основной текст (любой язык)",
  "source_language": "ISO 639-1 код (опционально, иначе определяется)",
  "preserve_quotes": false
}
```

### Выход

```json
{
  "title_ru": "Русский заголовок",
  "body_ru": "Полный перевод тела",
  "detected_language": "en",
  "untranslated_terms": ["IRS", "MiCAR"],
  "translator_notes": []
}
```

## Алгоритм

1. **Определи язык** оригинала (если не задан явно).
2. **Идентифицируй термины** из глоссария `references/glossary.md` и `references/organizations.md`.
3. **Переведи** с применением правил терминологии:
   - Аббревиатуры — сохраняй в оригинальной форме.
   - Названия организаций — используй официальные русские названия из реестра.
   - Денежные суммы — сохраняй формат: «$1 млрд», «€500 млн».
   - Имена собственные людей — транслитерируй по правилам или используй устоявшиеся варианты.
4. **Сохрани прямую речь** в кавычках с атрибуцией.
5. **Проверь** сохранение фактической информации: все цифры, даты, имена должны остаться неизменными.

## Правила

- **Не переводи аббревиатуры:** SEC, MAS, CBDC, DeFi, NFT, DAO, ICO, IPO, AML/CFT.
- **При первом упоминании организации** давай и оригинальное название, и русское: «Комиссия по ценным бумагам и биржам США (SEC)».
- **Не калькируй идиоматику** оригинала. «Game changer» — не «изменитель игры», а «революционное решение» (или, если можно, переформулировать).
- **Стиль — нейтральный новостной регистр.** Без художественных оборотов.
- **Длинные цитаты длиннее 15 слов** допускается передавать пересказом, не переводом.

## Справочники

- `references/glossary.md` — крипто-термины EN/RU.
- `references/organizations.md` — официальные русские названия регуляторов и крупных компаний.

## Примеры

### Пример 1

Вход:
```json
{"title": "MAS issues new licensing framework for stablecoin issuers", "body": "The Monetary Authority of Singapore (MAS) today announced a new regulatory framework for stablecoin issuers operating in Singapore. Under the new rules, issuers must maintain 100% reserves in low-risk assets and undergo monthly audits.", "source_language": "en"}
```

Выход:
```json
{
  "title_ru": "MAS вводит новые лицензионные требования для эмитентов стейблкоинов",
  "body_ru": "Денежно-кредитное управление Сингапура (MAS) сегодня объявило о новой регуляторной рамке для эмитентов стейблкоинов, работающих в Сингапуре. По новым правилам, эмитенты обязаны поддерживать 100% резервов в низкорисковых активах и проходить ежемесячный аудит.",
  "detected_language": "en",
  "untranslated_terms": ["MAS"],
  "translator_notes": []
}
```

### Пример 2 (терминологический вызов)

Вход:
```json
{"title": "Aave launches GHO stablecoin on Ethereum mainnet", "body": "Aave, a leading DeFi protocol, today launched its native overcollateralized stablecoin GHO on Ethereum mainnet. GHO is pegged to the US dollar and backed by a basket of assets supplied to Aave.", "source_language": "en"}
```

Выход:
```json
{
  "title_ru": "Aave запустил стейблкоин GHO в основной сети Ethereum",
  "body_ru": "Один из ведущих DeFi-протоколов Aave запустил собственный сверхобеспеченный стейблкоин GHO в основной сети Ethereum. GHO привязан к доллару США и обеспечен корзиной активов, переданных в Aave в качестве залога.",
  "detected_language": "en",
  "untranslated_terms": ["Aave", "GHO", "DeFi", "Ethereum"],
  "translator_notes": ["'Overcollateralized' переведено как 'сверхобеспеченный' (устоявшийся термин в DeFi)."]
}
```

## Антипримеры

❌ «Securities and Exchange Commission» → «Комиссия безопасности и обмена» (буквальный перевод вместо устоявшегося «Комиссия по ценным бумагам и биржам»).

❌ «Bitcoin's market cap reached $1 trillion» → «Рыночная кепка биткоина достигла $1 триллион» (плохая калька; правильно «капитализация»).

❌ «Federal Reserve raised rates» → «Федеральный резерв поднял оценки» (вместо «ФРС повысила ставки»).

## Граничные случаи

- **Текст частично на разных языках** → переводи только нерусские части.
- **Текст на казахском** → переводи; в notes отметь, что источник на казахском.
- **Технические термины без устоявшегося перевода** → транслитерируй и сохрани оригинал в скобках: «слэшинг (slashing)».
- **Не уверен в переводе названия закона/нормативного акта** → оставь оригинал и добавь в notes.

## Версионирование

- **1.0.0** — первая версия с базовым глоссарием.
