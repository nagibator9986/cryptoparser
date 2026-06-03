---
name: crypto-news-prioritizer
description: Use this skill whenever Claude needs to assess the importance of a crypto / digital-assets news article for a daily corporate digest. Output is one of low / medium / high / critical with a numerical score. Triggers include phrases such as 'rank these news', 'prioritize this article', 'which is most important', 'should this be on top of the digest', 'оцени значимость', 'насколько важно'. Use whenever the user is building a curated digest and needs an importance score that incorporates geographic priority (Kazakhstan first, CIS / Central Asia second, rest of world third). The skill bumps geo-priority-1 items up by one level when all else is equal. Kazakh and CIS legislative actions are escalated automatically; major regional crypto forums are treated as high-signal events. Mid-tier US/EU enforcement against unknown private entities is downgraded.
version: 1.1.0
license: Proprietary
---

# crypto-news-prioritizer

## Назначение

Skill оценивает значимость публикации для корпоративной ежедневной сводки и возвращает уровень приоритета (low/medium/high/critical) с числовой оценкой 0–100 и обоснованием. Geo-priority 1 (РК) повышает оценку на одну ступень при прочих равных.

## Когда использовать

- При сортировке публикаций внутри секций сводки.
- Когда объём публикаций превышает лимит сводки и нужно отобрать самое важное.
- При принятии решения, отправлять ли срочное уведомление получателям (только critical).

## Контракт

### Вход

```json
{
  "title": "Заголовок",
  "summary": "Реферат (если уже есть)",
  "topics": ["regulation", "cbdc"],
  "country": "KZ",
  "geo_priority": 1,
  "source_name": "nationalbank.kz",
  "key_entities": ["Национальный банк РК"],
  "is_legislative": false,
  "legislative_stage": null,
  "event_date": null,
  "event_location": null,
  "event_scale": null
}
```

Поля `is_legislative`, `legislative_stage`, `event_date`, `event_location`,
`event_scale` опциональны (поступают от классификатора). Используются как
сигналы автоматического апа: см. раздел «Алгоритм».

### Выход

```json
{
  "priority": "high",
  "score": 78,
  "geo_bumped": true,
  "reasoning": "Официальный релиз регулятора о CBDC; стандартная значимость medium → high из-за geo-priority 1."
}
```

Поля:
- **priority** — одно из: `low`, `medium`, `high`, `critical`.
- **score** — целое число 0–100.
- **geo_bumped** — true, если приоритет был повышен из-за geo_priority=1.
- **reasoning** — 1–2 предложения, обосновывающих оценку.

## Шкала значимости

### critical (score 85–100)

События, требующие немедленного внимания:
- **Законодательные действия в РК по цифровым активам на любой стадии**
  (внесение в Мажилис, чтения, подписание Президентом, вступление в силу) —
  даже если новость технически рутинная.
- Банкротства или критические инциденты крупных игроков (биржи с TVL/объёмами $1B+, эмитенты ключевых стейблкоинов).
- Взломы / эксплойты на сумму $100M+ против крупных, известных игроков.
- Острые регуляторные действия глобального значения (например, депег USDT/USDC, санкции против Tether/Circle).
- Любые официальные действия НБРК или AFSA в области цифровых активов.

### high (score 65–84)

Значимые события, важные для корпоративной осведомлённости:
- Законодательные действия в СНГ/ЦА по цифровым активам (Россия, Узбекистан,
  Беларусь и т. д. — любая стадия).
- Официальные **позиции и руководящие документы** крупных регуляторов (SEC, MAS,
  FCA, BaFin) — *обзор политики*, *новые правила*, *публичные рекомендации*.
  Иски против частных непубличных компаний сюда не относятся (см. low).
- Запуски CBDC-пилотов в значимых юрисдикциях.
- Запуски криптопродуктов крупными банками.
- Получение лицензий крупными игроками (например, MiCA для крупного эмитента).
- Отзыв лицензий или иски против крупных бирж из TOP-30 индустрии
  (Binance, Coinbase, Kraken, Bybit, OKX, Bitstamp, Gemini и т. п.).
- **Крупные мероприятия в РК** (`event_scale: kz_major`): Astana Finance Days,
  Digital Bridge с участием AFSA/НБРК/системообразующих банков.

### medium (score 40–64)

События общего интереса:
- Заявления экспертов и руководителей компаний.
- Технические обновления крупных платформ (хардфорки Ethereum).
- Средние запуски и партнёрства.
- Изменения политик в крупных компаниях.
- Региональные регуляторные новости среднего масштаба.
- **Крупные мероприятия в СНГ** (`event_scale: cis_major`): Blockchain Life
  Moscow, региональные саммиты с участием крупных бирж/банков.

### low (score 0–39)

Малозначимое:
- Обзоры рынка и прогнозы цен.
- Мнения и аналитические колонки.
- Мелкие технические обновления.
- Слухи и неподтверждённые сообщения (если не критичные).
- **Действия иностранных регуляторов (US/EU/GB) против непубличных
  компаний или площадок третьих стран** — любого типа: иски, штрафы,
  санкции OFAC, заморозки активов, обвинения DOJ. Если ответчик не входит
  в TOP-50 индустрии и не является эмитентом значимого стейблкоина или
  крупным DeFi-протоколом, событие даёт **low**. Сюда относятся:
  иранские/северокорейские/венесуэльские биржи (Nobitex, Wallex и т. п.),
  мелкие частные фонды, безымянные dark-net операторы. См.
  `references/aggregator-foreign-enforcement.md`. Низкий сигнал для банка
  РК: ни регуляторная повестка, ни клиентская, ни глобальные рынки не
  затрагиваются.
- **Мелкие мероприятия** (`event_scale: minor` или отсутствует) — митапы,
  локальные воркшопы. Если классификатор всё же присвоил тег `events`
  без подтверждённого масштаба → score < 20.

## Алгоритм

1. **Базовая оценка** по типу события и масштабу. Используй критерии шкалы.
2. **Авто-эскалация для законодательства:**
   - `is_legislative=true` и `geo_priority=1` → **critical** (score ≥ 88),
     независимо от стадии.
   - `is_legislative=true` и `geo_priority=2` → **high** (score ≥ 72).
   - `is_legislative=true` и `geo_priority=3` → не повышать; обычная шкала.
3. **Авто-оценка для мероприятий:**
   - `event_scale=kz_major` → **high** (score ≥ 70).
   - `event_scale=cis_major` → **medium** (score ≥ 50).
   - `event_scale=global_major` → **medium** (score ≥ 45).
   - `event_scale=minor` или отсутствует при `topics ∋ events` → **low** (<20).
4. **Анти-усиление для агрегаторных перепечаток.** Если новость — это
   перепечатка казахстанским/российским агрегатором (ForkLog, Bits.media,
   Incrypted, Decenter и т. п.) **иска US/EU регулятора против непубличной
   мелкой компании**, не подгоняй её под «official regulator stance». Это
   рутинный enforcement, не политическая позиция регулятора. См.
   `references/aggregator-foreign-enforcement.md`.
5. **Поправка на geo_priority:**
   - geo_priority=1 (РК) → при пограничном score (например, на границе medium/high) → поднимай на один уровень. Установи `geo_bumped: true`.
   - geo_priority=2 (СНГ) → без изменений.
   - geo_priority=3 (мир) → без изменений; при сомнении понижай.
6. **Поправка на источник.** Официальный регулятор → +5 к score; соцсети без подтверждения → −10.
7. **Поправка на тип топика:** `regulation`, `cbdc`, `licensing`, `security-incidents` для финансового банковского контекста — естественные high. `market-events` для банка обычно low.
8. **Сформулируй reasoning.**

## Правила

- **Не натягивай critical** на обычные события — теряет смысл срочный канал доставки.
- **РК всегда приоритетна,** но не за счёт ложных срабатываний.
- **Геоприоритет — не единственный критерий.** Мелкая новость о Казахстане не должна обгонять глобальное критическое событие.
- **Законодательство РК / СНГ — особый случай.** Даже технические поправки
  поднимаются до critical/high автоматически (см. Алгоритм п. 2). Банк
  должен реагировать на правовое поле, а не на громкость новости.
- **Мероприятия → строго по масштабу.** `event_scale` — единственный
  критерий. Без поля или с `minor` → low. Не повышай только потому, что
  город — Алматы или Астана.

## Справочники

- `references/aggregator-foreign-enforcement.md` — критерии анти-усиления
  для перепечаток мелких иностранных enforcement-кейсов.

## Примеры

### Пример 1: казахстанский CBDC — high благодаря geo bump

Вход:
```json
{"title": "НБРК запустил вторую фазу цифрового тенге", "topics": ["cbdc", "regulation"], "country": "KZ", "geo_priority": 1, "source_name": "nationalbank.kz"}
```

Выход:
```json
{"priority": "high", "score": 76, "geo_bumped": true, "reasoning": "Запуск пилота CBDC в РК; базово medium, повышено до high из-за geo-priority 1 и официального источника."}
```

### Пример 2: глобальная критическая новость

Вход:
```json
{"title": "Tether USDT депег: курс упал до $0.94", "topics": ["stablecoins", "security-incidents"], "country": "INT", "geo_priority": 3, "source_name": "coindesk.com"}
```

Выход:
```json
{"priority": "critical", "score": 92, "geo_bumped": false, "reasoning": "Депег ключевого глобального стейблкоина — событие критической значимости для всей индустрии, независимо от географии."}
```

### Пример 3: рутинный материал — low

Вход:
```json
{"title": "Аналитики предсказывают рост биткоина к концу года", "topics": ["market-events"], "country": "INT", "geo_priority": 3, "source_name": "cointelegraph.com"}
```

Выход:
```json
{"priority": "low", "score": 18, "geo_bumped": false, "reasoning": "Прогноз цены без фактического события; малая значимость для банковского контекста."}
```

### Пример 4: законопроект РК — автоматический critical

Вход:
```json
{"title": "В Мажилис внесён законопроект о цифровых активах", "topics": ["regulation"], "country": "KZ", "geo_priority": 1, "is_legislative": true, "legislative_stage": "introduced", "source_name": "gov.kz", "key_entities": ["Мажилис РК"]}
```

Выход:
```json
{"priority": "critical", "score": 90, "geo_bumped": false, "reasoning": "Законодательная инициатива по цифровым активам в РК — авто-эскалация до critical (стадия introduced)."}
```

### Пример 5: мелкий US-кейс — low (анти-усиление)

Вход:
```json
{"title": "SEC подала иск против основателя Privvy Investments за криптосхему на $12,3 млн", "topics": ["regulation", "security-incidents"], "country": "US", "geo_priority": 3, "source_name": "forklog.com", "key_entities": ["SEC", "Privvy Investments", "Натан Фуллер"]}
```

Выход:
```json
{"priority": "low", "score": 24, "geo_bumped": false, "reasoning": "Иск SEC против непубличной компании с привлечённым капиталом $12 млн и ~150 потерпевшими. Ответчик не входит в TOP-50 индустрии; перепечатка агрегатором. Низкий сигнал для банка РК."}
```

### Пример 5b: OFAC-санкции против иранских бирж — low

Вход:
```json
{"title": "США ввели санкции против четырёх иранских криптобирж, включая Nobitex", "topics": ["regulation"], "country": "US", "geo_priority": 3, "source_name": "forklog.com", "key_entities": ["OFAC", "Nobitex", "Wallex", "Bitpin", "Ramzinex"]}
```

Выход:
```json
{"priority": "low", "score": 22, "geo_bumped": false, "reasoning": "OFAC-санкции против иранских бирж третьего уровня. Ответчики не входят в TOP-50, оперируют вне KZ/CIS — нет влияния на банк РК и его клиентов. Анти-усиление по references/aggregator-foreign-enforcement.md."}
```

### Пример 6: крупный форум в Астане — high

Вход:
```json
{"title": "AIFC проведёт Astana Finance Days 12-14 ноября", "topics": ["events", "regulation"], "country": "KZ", "geo_priority": 1, "event_date": "2025-11-12/2025-11-14", "event_location": "Астана, AIFC", "event_scale": "kz_major", "source_name": "aifc.kz", "key_entities": ["AIFC", "AFSA", "НБРК"]}
```

Выход:
```json
{"priority": "high", "score": 76, "geo_bumped": false, "reasoning": "Крупный отраслевой форум в РК с участием AFSA и НБРК — авто-оценка high по event_scale=kz_major."}
```

## Антипримеры

❌ `priority: critical` для запуска маленького криптопродукта в РК только потому, что это РК.

❌ `priority: low` для крупного штрафа SEC против крупной биржи только потому, что geo_priority=3.

❌ `priority: high` для иска SEC против неизвестной частной компании с
$10–30 млн привлечённого капитала, перепечатанного криптоагрегатором.
Это рутинный enforcement, не «official regulator stance».

❌ `priority: high` для OFAC-санкций против иранских/северокорейских
криптобирж третьего уровня, перепечатанных агрегатором. Третья страна,
не TOP-50, не затрагивает банк РК — это low.

❌ `priority: high` для мелкого локального митапа на 20 человек, даже если
он проводится в Алматы.

## Граничные случаи

- **Нет тегов (`topics: []`)** → это сигнал, что классификатор не нашёл темы. Скорее всего `priority: low`, score < 30.
- **Несколько критических тегов одновременно** → проверь, что событие действительно критическое, не складывай оценки автоматически.
- **Спорный масштаб** → проверь key_entities; если упомянуты ТОП-игроки — выше, если неизвестные — ниже.

## Версионирование

- **1.0.0** — первая версия. 4-уровневая шкала; geo-bump на одну ступень.
- **1.1.0** — добавлены авто-эскалация для законодательства РК/СНГ,
  авто-оценка для мероприятий по `event_scale`, анти-усиление для
  агрегаторных перепечаток мелких US/EU enforcement-кейсов. Новые
  опциональные поля входа: `is_legislative`, `legislative_stage`,
  `event_date`, `event_location`, `event_scale`. Совместимо со старым
  контрактом — поля можно не передавать.
