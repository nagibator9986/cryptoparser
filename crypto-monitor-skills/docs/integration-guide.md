# Integration guide — подключение Claude Skills к Anthropic API

## Контекст

Skills из этого каталога — это **prompt-based skills**, то есть продвинутые системные промпты с приложениями (references, assets, evals). Они подключаются к Claude через стандартный Anthropic API: содержимое `SKILL.md` подаётся как `system`-промпт, содержимое `references/` и `assets/` — при необходимости как вложенный текст в `system` или как user-сообщения.

Это намеренно простой подход — он не требует загрузки skills в Anthropic Platform и не требует никаких новых SDK. Минимум зависимостей, максимум контроля.

## Требования

- Python 3.11+
- `anthropic` SDK >= 0.40
- Активный API-ключ Anthropic (https://console.anthropic.com)

```bash
pip install anthropic
```

## Базовый пример: классификация одной публикации

```python
import anthropic
import json
from pathlib import Path

SKILLS_ROOT = Path("crypto-monitor-skills")

def load_skill(skill_name: str, include_references: bool = True) -> str:
    """
    Загружает SKILL.md и (опционально) все references/ файлы как единый
    системный промпт. References добавляются с разделителями.
    """
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    parts = [skill_md]

    if include_references:
        refs_dir = skill_dir / "references"
        if refs_dir.exists():
            for ref_file in sorted(refs_dir.glob("*.md")):
                parts.append(f"\n\n---\n# REFERENCE: {ref_file.name}\n\n")
                parts.append(ref_file.read_text(encoding="utf-8"))

    return "".join(parts)


client = anthropic.Anthropic()

# Подгружаем skill
system_prompt = load_skill("crypto-news-classifier", include_references=True)

# Готовим входные данные
article = {
    "title": "Национальный банк Казахстана объявил о запуске второй фазы пилота цифрового тенге",
    "body": "Алматы. Национальный банк Республики Казахстан запустил вторую фазу пилотного проекта цифрового тенге. К пилоту присоединились четыре коммерческих банка: Halyk, Kaspi, Forte и Jusan...",
    "source_name": "nationalbank.kz",
    "source_url": "https://nationalbank.kz/news/123",
    "language": "ru"
}

response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    system=system_prompt,
    messages=[
        {
            "role": "user",
            "content": f"Classify this news. Return JSON only.\n\nInput:\n{json.dumps(article, ensure_ascii=False, indent=2)}"
        }
    ]
)

# Парсим JSON-ответ
result_text = response.content[0].text
result = json.loads(result_text)
print(result)
```

Ожидаемый вывод:
```json
{
  "topics": ["cbdc", "regulation"],
  "country": "KZ",
  "geo_priority": 1,
  "confidence": 0.95,
  "reasoning": "Официальный релиз Национального банка РК о пилоте цифрового тенге."
}
```

## Полный пайплайн (последовательный)

```python
import anthropic
import json
from pathlib import Path
from typing import Any

SKILLS_ROOT = Path("crypto-monitor-skills")
client = anthropic.Anthropic()

def load_skill(name: str) -> str:
    skill_dir = SKILLS_ROOT / name
    parts = [(skill_dir / "SKILL.md").read_text(encoding="utf-8")]
    refs = skill_dir / "references"
    if refs.exists():
        for f in sorted(refs.glob("*.md")):
            parts.append(f"\n\n---\n# REFERENCE: {f.name}\n\n{f.read_text(encoding='utf-8')}")
    return "".join(parts)


def call_skill(skill_name: str, input_data: dict, model: str = "claude-opus-4-5") -> dict:
    """Вызов skill: подгружаем SKILL.md, отправляем input, парсим JSON."""
    system_prompt = load_skill(skill_name)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"Process this input according to the skill. Return JSON only, no preamble.\n\nInput:\n{json.dumps(input_data, ensure_ascii=False)}"
        }]
    )
    text = response.content[0].text.strip()
    # Срезаем возможные ```json ... ``` обёртки
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def process_one_article(article: dict) -> dict:
    """Полный пайплайн обработки одной публикации."""
    result = dict(article)

    # 1. Перевод (если не русский)
    if article["language"] != "ru":
        translated = call_skill("crypto-news-translator", article)
        result["title"] = translated["title_ru"]
        result["body"] = translated["body_ru"]

    # 2. Классификация
    classified = call_skill("crypto-news-classifier", result)
    result.update(classified)

    # 3. Реферат
    summarized = call_skill("crypto-news-summarizer", result)
    result.update(summarized)

    # 4. Приоритизация
    prioritized = call_skill("crypto-news-prioritizer", result)
    result.update(prioritized)

    return result


def build_digest(articles: list[dict], digest_date: str) -> dict:
    """Дедупликация → сборка → QA."""
    # Дедупликация
    dedup = call_skill("crypto-news-deduplicator", {"articles": articles})

    # Берём только канонические
    canonical_ids = {c["canonical_id"] for c in dedup["clusters"]} | set(dedup["singletons"])
    canonical_articles = [a for a in articles if a["id"] in canonical_ids]

    # Сборка
    digest = call_skill("crypto-digest-builder", {
        "digest_date": digest_date,
        "articles": canonical_articles,
        "max_items_per_section": 5,
        "total_max_items": 25
    })

    # QA
    qa = call_skill("crypto-digest-quality-check", {
        **digest,
        "articles": canonical_articles
    })

    return {"digest": digest, "qa": qa}


# Пример использования
if __name__ == "__main__":
    raw_articles = [
        {
            "id": "art_001",
            "title": "НБРК запустил вторую фазу цифрового тенге",
            "body": "...",
            "source_name": "nationalbank.kz",
            "source_url": "https://nationalbank.kz/news/123",
            "language": "ru",
            "published_at": "2025-03-12T08:00:00+05:00"
        },
        # ...
    ]

    # Обработка по одной
    processed = [process_one_article(a) for a in raw_articles]

    # Сборка сводки
    result = build_digest(processed, digest_date="2025-03-12")

    if result["qa"]["passed"]:
        print("Сводка готова к отправке.")
        # отправка через SMTP / Telegram
    else:
        print("Сводка не прошла QA:", result["qa"]["issues"])
```

## Параллельный пайплайн (для производительности)

При обработке десятков публикаций каждый день имеет смысл распараллелить вызовы:

```python
import asyncio
from anthropic import AsyncAnthropic

aclient = AsyncAnthropic()

async def acall_skill(skill_name: str, input_data: dict) -> dict:
    system_prompt = load_skill(skill_name)
    response = await aclient.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Process. Return JSON only.\n\nInput:\n{json.dumps(input_data, ensure_ascii=False)}"}]
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


async def aprocess_one_article(article: dict) -> dict:
    # Перевод и классификация могут запускаться параллельно? Нет — классификатор зависит от перевода.
    # Реферат и приоритизация запускаются после классификации.
    result = dict(article)

    if article["language"] != "ru":
        translated = await acall_skill("crypto-news-translator", article)
        result.update({"title": translated["title_ru"], "body": translated["body_ru"]})

    classified = await acall_skill("crypto-news-classifier", result)
    result.update(classified)

    summarized = await acall_skill("crypto-news-summarizer", result)
    result.update(summarized)

    prioritized = await acall_skill("crypto-news-prioritizer", result)
    result.update(prioritized)

    return result


async def aprocess_batch(articles: list[dict]) -> list[dict]:
    # Все статьи обрабатываются параллельно
    return await asyncio.gather(*(aprocess_one_article(a) for a in articles))


# Запуск
articles_processed = asyncio.run(aprocess_batch(raw_articles))
```

## Использование Message Batches API (для дешёвой пакетной обработки)

При обработке сотен публикаций за раз и допустимой задержке в часах используйте Batches API (50% скидка от обычной цены):

```python
import anthropic
client = anthropic.Anthropic()

batch_requests = [
    {
        "custom_id": f"article_{a['id']}",
        "params": {
            "model": "claude-opus-4-5",
            "max_tokens": 1024,
            "system": load_skill("crypto-news-classifier"),
            "messages": [{
                "role": "user",
                "content": f"Classify this news. Return JSON only.\n\nInput:\n{json.dumps(a, ensure_ascii=False)}"
            }]
        }
    }
    for a in raw_articles
]

batch = client.messages.batches.create(requests=batch_requests)
print(f"Batch created: {batch.id}, status: {batch.processing_status}")

# Опрос статуса, затем получение результатов
# результаты доступны через client.messages.batches.results(batch.id)
```

## Конфигурация и безопасность

### Хранение API-ключа

Никогда не храните API-ключ в коде или конфигах. Используйте:
- `ANTHROPIC_API_KEY` переменную окружения (SDK подхватит автоматически).
- HashiCorp Vault, AWS Secrets Manager или аналог.

### Лимиты и ретраи

SDK имеет встроенные ретраи. Для продакшна рекомендуется:

```python
client = anthropic.Anthropic(
    max_retries=3,
    timeout=60.0
)
```

При rate-limit (429) — экспоненциальный backoff.

### Логирование

Логируйте каждый вызов skill: имя skill, размер входа, размер выхода, латентность, расход токенов (доступен в `response.usage`). Это критично для:
- Контроля стоимости.
- Расследования некачественных результатов.
- Аудита.

```python
import logging
logger = logging.getLogger(__name__)

def call_skill_logged(skill_name, input_data):
    start = time.time()
    response = client.messages.create(...)
    duration = time.time() - start
    logger.info({
        "skill": skill_name,
        "input_chars": len(json.dumps(input_data)),
        "output_chars": len(response.content[0].text),
        "duration_s": duration,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens
    })
    return response
```

## Прогон evals

Каждый skill содержит `evals/evals.json`. Простой раннер:

```python
import json
from pathlib import Path

def run_evals(skill_name: str):
    evals = json.loads((SKILLS_ROOT / skill_name / "evals" / "evals.json").read_text(encoding="utf-8"))
    passed = 0
    failed = 0

    for case in evals["evals"]:
        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=2048,
                system=load_skill(skill_name),
                messages=[{"role": "user", "content": case["prompt"]}]
            )
            output = response.content[0].text.strip()
            # Здесь — проверка assertions; в простом случае — глазами
            print(f"✓ Case {case['id']} ({case['name']})")
            print(f"  Output: {output[:200]}...")
            passed += 1
        except Exception as e:
            print(f"✗ Case {case['id']} ({case['name']}): {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} passed")


# Прогон
run_evals("crypto-news-classifier")
```

## Дальнейшие шаги

1. Развернуть пайплайн в Docker по схеме из ТЗ.
2. Подключить мониторинг (расход токенов, латентность skills).
3. Завести регулярные регрессионные прогоны evals (например, ежедневно).
4. Создать дашборд качества: доля успешно классифицированных, среднее время сборки, средний score публикаций.
