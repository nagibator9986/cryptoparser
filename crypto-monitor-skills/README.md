# Crypto Monitor Skills

Набор Claude Skills для системы автоматизированного мониторинга индустрии цифровых активов и криптоиндустрии (банк РК).

Документ ТЗ, на основании которого создан этот пакет — `ТЗ_Мониторинг_цифровых_активов.docx`.

## Состав

Восемь специализированных skills, каждый отвечает за один этап конвейера обработки публикаций:

| Skill | Назначение |
|-------|------------|
| `crypto-news-classifier` | Классификация публикации: тематические теги, страна, геоприоритет |
| `crypto-news-translator` | Перевод иноязычных публикаций на русский с сохранением терминологии |
| `crypto-news-deduplicator` | Дедупликация и кластеризация публикаций об одном событии |
| `crypto-news-summarizer` | Краткий реферат на русском (60–120 слов) с защитой от копирайта |
| `crypto-news-prioritizer` | Оценка значимости (low/medium/high/critical) с учётом гео |
| `crypto-digest-builder` | Сборка сводки в HTML / plain text / Telegram MarkdownV2 |
| `crypto-digest-quality-check` | Финальный QA-контроль перед отправкой |
| `crypto-source-validator` | Оценка нового источника на пригодность |

## Структура каталога

```
crypto-monitor-skills/
├── README.md                              ← вы здесь
├── MANIFEST.json                          ← реестр skills с версиями
├── docs/
│   ├── integration-guide.md               ← как подключить к Anthropic API
│   └── skill-development-guide.md         ← как развивать skills
├── crypto-news-classifier/
│   ├── SKILL.md
│   ├── references/
│   │   ├── taxonomy.md
│   │   └── geo-priorities.md
│   └── evals/
│       └── evals.json
├── crypto-news-translator/
│   ├── SKILL.md
│   ├── references/
│   │   ├── glossary.md
│   │   └── organizations.md
│   └── evals/
│       └── evals.json
├── crypto-news-deduplicator/
│   ├── SKILL.md
│   └── evals/
│       └── evals.json
├── crypto-news-summarizer/
│   ├── SKILL.md
│   ├── references/
│   │   ├── copyright-rules.md
│   │   └── summary-template.md
│   └── evals/
│       └── evals.json
├── crypto-news-prioritizer/
│   ├── SKILL.md
│   └── evals/
│       └── evals.json
├── crypto-digest-builder/
│   ├── SKILL.md
│   ├── assets/
│   │   ├── email-template.html
│   │   └── telegram-template.md
│   ├── references/
│   │   └── style-guide.md
│   └── evals/
│       └── evals.json
├── crypto-digest-quality-check/
│   ├── SKILL.md
│   ├── references/
│   │   └── checklist.md
│   └── evals/
│       └── evals.json
└── crypto-source-validator/
    ├── SKILL.md
    └── evals/
        └── evals.json
```

## Пайплайн обработки

```
[Источник] → ingest → normalize → [language detect]
                                       ↓
                          [crypto-news-translator] (если не RU)
                                       ↓
                          [crypto-news-classifier]
                                       ↓
                          [crypto-news-deduplicator]
                                       ↓
                          [crypto-news-summarizer]
                                       ↓
                          [crypto-news-prioritizer]
                                       ↓
                          [crypto-digest-builder]
                                       ↓
                          [crypto-digest-quality-check]
                                       ↓
                          [delivery: email + Telegram]
```

`crypto-source-validator` вызывается отдельно при добавлении новых источников.

## Подключение

См. `docs/integration-guide.md` для подробного руководства.

Краткий пример (Python, `anthropic` SDK):

```python
import anthropic
from pathlib import Path

client = anthropic.Anthropic()

def load_skill(skill_name: str) -> str:
    return (Path("crypto-monitor-skills") / skill_name / "SKILL.md").read_text(encoding="utf-8")

system_prompt = load_skill("crypto-news-classifier")

response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    system=system_prompt,
    messages=[{"role": "user", "content": "Classify this news. Input: {...}"}]
)
print(response.content[0].text)
```

## Принципы

- Каждый skill — самодостаточный модуль с собственной документацией, справочниками и evals.
- Skill ссылается на свои references/ и assets/, но не на другие skills.
- YAML-фронтматтер каждого SKILL.md содержит «pushy» description с явными триггерными фразами.
- Все skills версионируются (поле `version` в фронтматтере).
- Минимум 3 теста (evals) в каждом skill, включая один граничный случай.

## Лицензия

Proprietary. Все права защищены.

## Версия пакета

1.0.0 — первая версия (см. MANIFEST.json для версий отдельных skills).
