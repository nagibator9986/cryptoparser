from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

from crypto_monitor.json_utils import JsonExtractionError, extract_json_object

logger = logging.getLogger(__name__)


class LlmClient(Protocol):
    def generate_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Generate a JSON object."""


class GeminiClient:
    """Thin Gemini adapter using the official Google Gen AI Python SDK."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is required for GeminiClient")

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency google-genai. Install with: pip install -e ."
            ) from exc

        self._types = types
        # http_options.timeout is milliseconds in the Gen AI SDK. Without it a
        # stalled request hangs the whole pipeline indefinitely.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.max_retries = max(1, int(max_retries))

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        prompt = user_prompt
        last_text = ""
        last_error: JsonExtractionError | None = None
        config = self._types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            # Skill calls are structured extraction, not multi-step
            # reasoning. The default thinking budget on gemini-2.5-flash
            # eats 600-3800 tokens per call (visible as
            # thoughts_token_count in logs) and slows wall time roughly
            # 30%. Pin to 0 to keep latency predictable.
            thinking_config=self._types.ThinkingConfig(thinking_budget=0),
        )
        for attempt in range(1, 3):
            text = self._generate_text(prompt, config, json_repair_attempt=attempt)
            try:
                return extract_json_object(text)
            except JsonExtractionError as exc:
                last_text = text
                last_error = exc
                prompt = (
                    f"{user_prompt}\n\n"
                    "Your previous response was not a valid JSON object. "
                    "Return exactly one JSON object that matches the contract. "
                    "Do not include prose, Markdown, XML, or HTML outside JSON.\n\n"
                    f"Previous response:\n{last_text[:4000]}"
                )

        assert last_error is not None
        raise last_error

    def _generate_text(self, prompt: str, config: Any, *, json_repair_attempt: int) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            start = time.perf_counter()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                if not _is_transient_error(exc) or attempt == self.max_retries:
                    raise
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                logger.warning(
                    "gemini_call_retry attempt=%s error=%s sleeping_s=%.1f",
                    attempt,
                    f"{type(exc).__name__}: {exc}",
                    delay,
                )
                time.sleep(delay)
                continue
            duration = time.perf_counter() - start
            text = getattr(response, "text", "") or ""
            usage = getattr(response, "usage_metadata", None)
            logger.info(
                "gemini_call model=%s repair_attempt=%s transient_attempt=%s "
                "duration_s=%.3f usage=%s chars=%s",
                self.model,
                json_repair_attempt,
                attempt,
                duration,
                usage,
                len(text),
            )
            return text
        assert last_error is not None
        raise last_error


class DryRunLlmClient:
    """Deterministic local client for syntax checks and demos without API calls."""

    def generate_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        prompt = user_prompt.lower()
        skill_name = self._detect_skill(system_prompt)
        if skill_name == "crypto-news-classifier":
            if "курс доллара" in prompt or "kase" in prompt:
                return {
                    "topics": [],
                    "country": "KZ",
                    "geo_priority": 0,
                    "confidence": 0.93,
                    "is_legislative": False,
                    "reasoning": "Публикация не относится к индустрии цифровых активов.",
                }
            if "мажилис" in prompt and ("законопроект" in prompt or "цифров" in prompt):
                return {
                    "topics": ["regulation"],
                    "country": "KZ",
                    "geo_priority": 1,
                    "confidence": 0.94,
                    "is_legislative": True,
                    "legislative_stage": "introduced",
                    "reasoning": (
                        "Dry-run: законопроект о цифровых активах внесён в Мажилис РК."
                    ),
                }
            if "astana finance days" in prompt or "форум" in prompt and "aifc" in prompt:
                return {
                    "topics": ["events", "regulation"],
                    "country": "KZ",
                    "geo_priority": 1,
                    "confidence": 0.95,
                    "is_legislative": False,
                    "event_date": "2025-11-12/2025-11-14",
                    "event_location": "Астана, AIFC",
                    "event_scale": "kz_major",
                    "reasoning": "Dry-run: крупный отраслевой форум в РК.",
                }
            if "privvy" in prompt or ("$12,3 млн" in prompt and "sec" in prompt):
                return {
                    "topics": ["regulation", "security-incidents"],
                    "country": "US",
                    "geo_priority": 3,
                    "confidence": 0.9,
                    "is_legislative": False,
                    "reasoning": "Dry-run: иск SEC против частной компании Privvy.",
                }
            if "nobitex" in prompt or ("ofac" in prompt and "иран" in prompt):
                return {
                    "topics": ["regulation"],
                    "country": "US",
                    "geo_priority": 3,
                    "confidence": 0.9,
                    "is_legislative": False,
                    "reasoning": "Dry-run: OFAC-санкции против иранских бирж.",
                }
            if "sec charges" in prompt or "sec.gov" in prompt:
                return {
                    "topics": ["regulation", "exchanges", "licensing"],
                    "country": "US",
                    "geo_priority": 3,
                    "confidence": 0.95,
                    "is_legislative": False,
                    "reasoning": "Dry-run: регуляторный кейс SEC против криптобиржи.",
                }
            if "afsa grants" in prompt or "afsa granted" in prompt:
                return {
                    "topics": ["licensing"],
                    "country": "KZ",
                    "geo_priority": 1,
                    "confidence": 0.6,
                    "is_legislative": False,
                    "reasoning": "Dry-run: AFSA относится к юрисдикции МФЦА в Казахстане.",
                }
            return {
                "topics": ["cbdc", "regulation"] if "цифров" in prompt or "cbdc" in prompt else [],
                "country": "KZ" if "казахстан" in prompt or "нбрк" in prompt else "INT",
                "geo_priority": 1 if "казахстан" in prompt or "нбрк" in prompt else 0,
                "confidence": 0.9,
                "is_legislative": False,
                "reasoning": "Dry-run классификация без вызова Gemini.",
            }
        if skill_name == "crypto-news-translator":
            if "aave" in prompt:
                return {
                    "title_ru": "Aave запустил стейблкоин GHO в Ethereum",
                    "body_ru": (
                        "Aave, DeFi-протокол, запустил сверхобеспеченный "
                        "стейблкоин GHO в основной сети Ethereum."
                    ),
                    "detected_language": "en",
                    "untranslated_terms": ["Aave", "GHO", "DeFi", "Ethereum"],
                    "translator_notes": [],
                }
            if "afsa" in prompt:
                return {
                    "title_ru": "AFSA выдало лицензию криптопровайдеру",
                    "body_ru": (
                        "AFSA выдало лицензию XYZ Crypto в рамках AIFC/МФЦА. "
                        "Лицензия сопоставима со стандартами MiCA."
                    ),
                    "detected_language": "en",
                    "untranslated_terms": ["AFSA", "AIFC", "MiCA", "XYZ Crypto"],
                    "translator_notes": [],
                }
            return {
                "title_ru": "MAS вводит требования для эмитентов стейблкоинов",
                "body_ru": (
                    "Денежно-кредитное управление Сингапура (MAS) объявило "
                    "правила для эмитентов стейблкоинов: 100% резервы и аудит."
                ),
                "detected_language": "en",
                "untranslated_terms": ["MAS"],
                "translator_notes": ["Dry-run режим."],
            }
        if skill_name == "crypto-news-summarizer":
            summary = (
                "Национальный банк РК сообщил о развитии проекта цифровой тенге. "
                "В материале указано, что к пилоту привлечены Halyk, Kaspi, Forte "
                "и Jusan, а тестирование рассчитано на 10 000 пользователей. "
                "Отдельный акцент сделан на офлайн-операциях и устойчивости "
                "платформы. Информация относится к регулированию и практическому "
                "внедрению CBDC в Казахстане. Dry-run текст сохраняет структуру "
                "ответа и не является редакционной версией для отправки. "
                "Финальная редакция должна быть создана Gemini."
            )
            if "bittrex" in prompt:
                summary = (
                    "Комиссия по ценным бумагам США (SEC) предъявила претензии "
                    "к Bittrex и бывшему руководителю компании. Регулятор считает, "
                    "что площадка предоставляла биржевые, брокерские и клиринговые "
                    "услуги без требуемой регистрации. В иске также указано, что "
                    "за 2014-2022 годы доход Bittrex от комиссий составил не менее "
                    "$1,3 млрд. Компания может оспаривать позицию регулятора в суде. "
                    "Dry-run текст нужен только для проверки формата и структуры "
                    "корпоративного реферата."
                )
            if "bloomx" in prompt:
                summary = (
                    "По сообщению пользователей в Telegram-канале, криптобиржа "
                    "BloomX временно приостановила вывод средств. На сайте биржи "
                    "нет отдельного официального уведомления, поэтому информация "
                    "требует осторожной оценки. По утверждению представителей "
                    "BloomX в социальных сетях, речь идёт о плановых технических "
                    "работах сроком до 24 часов. До появления официального релиза "
                    "событие следует считать неподтверждённым. Dry-run текст "
                    "используется только для проверки формата и структуры ответа, "
                    "а не для реальной публикации получателям."
                )
            if "xyz crypto" in prompt:
                summary = (
                    "Агентство по регулированию финансовых услуг МФЦА (AFSA) "
                    "12 марта выдало лицензию криптопровайдеру XYZ Crypto на "
                    "оказание услуг хранения цифровых активов. Событие относится "
                    "к направлению лицензирования и регулирования в юрисдикции "
                    "МФЦА. В исходном сообщении не указаны суммы, количество "
                    "клиентов или дополнительные коммерческие условия. Поэтому "
                    "реферат ограничивается подтверждёнными фактами и не добавляет "
                    "непроверенные детали. Dry-run текст предназначен для теста "
                    "и последующей замены реальным ответом Gemini."
                )
            return {
                "title_ru": "Dry-run реферат публикации",
                "summary": summary,
                "word_count": len(summary.split()),
                "key_entities": ["AFSA", "XYZ Crypto"] if "xyz crypto" in prompt else [],
                "warnings": ["Источник требует проверки."] if "bloomx" in prompt else [],
            }
        if skill_name == "crypto-news-prioritizer":
            if "ranked_articles" in prompt and '"articles"' in prompt:
                ids = re.findall(r'"id":\s*"([^"]+)"', user_prompt)
                ranked = []
                kz_markers = ("afsa", "nationalbank", "нбрк")
                for index, article_id in enumerate(dict.fromkeys(ids)):
                    article_window = _window_around_id(user_prompt.lower(), article_id.lower())
                    is_legislative = '"is_legislative": true' in article_window
                    event_scale = _extract_event_scale(article_window)
                    is_privvy = "privvy" in article_window
                    geo_1 = '"geo_priority": 1' in article_window
                    geo_2 = '"geo_priority": 2' in article_window

                    if is_legislative and geo_1:
                        priority, score = "critical", 92 - index
                    elif is_legislative and geo_2:
                        priority, score = "high", 78 - index
                    elif event_scale == "kz_major":
                        priority, score = "high", 75 - index
                    elif event_scale == "cis_major":
                        priority, score = "medium", 55 - index
                    elif is_privvy or "nobitex" in article_window or (
                        "ofac" in article_window and "иран" in article_window
                    ):
                        priority, score = "low", 22 - index
                    elif any(marker in article_window for marker in kz_markers):
                        priority, score = "high", 90 - index
                    elif "sec" in article_window or "security" in article_window:
                        priority, score = "high", 82 - index
                    else:
                        priority, score = "medium", 60 - index
                    ranked.append(
                        {
                            "id": article_id,
                            "priority": priority,
                            "score": max(score, 1),
                            "ranking_reason": "Dry-run cross-article Gemini ranking.",
                        }
                    )
                ranked.sort(
                    key=lambda item: (
                        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                            item["priority"], 4
                        ),
                        -int(item["score"]),
                    )
                )
                return {"ranked_articles": ranked, "dropped_ids": []}
            if "is_legislative\": true" in prompt and "geo_priority\": 1" in prompt:
                return {
                    "priority": "critical",
                    "score": 90,
                    "geo_bumped": False,
                    "reasoning": "Dry-run: законопроект РК — авто-эскалация до critical.",
                }
            if "is_legislative\": true" in prompt and "geo_priority\": 2" in prompt:
                return {
                    "priority": "high",
                    "score": 74,
                    "geo_bumped": False,
                    "reasoning": "Dry-run: законопроект СНГ — авто-эскалация до high.",
                }
            if "event_scale\": \"kz_major\"" in prompt:
                return {
                    "priority": "high",
                    "score": 75,
                    "geo_bumped": False,
                    "reasoning": "Dry-run: крупное мероприятие в РК — auto high.",
                }
            if "privvy" in prompt:
                return {
                    "priority": "low",
                    "score": 22,
                    "geo_bumped": False,
                    "reasoning": (
                        "Dry-run: перепечатка мелкого US enforcement "
                        "(Privvy) — анти-усиление."
                    ),
                }
            if "nobitex" in prompt or ("ofac" in prompt and "иран" in prompt):
                return {
                    "priority": "low",
                    "score": 20,
                    "geo_bumped": False,
                    "reasoning": (
                        "Dry-run: OFAC-санкции против иранских бирж "
                        "(третья страна, не TOP-50) — анти-усиление."
                    ),
                }
            if "депег" in prompt or "$0.94" in prompt:
                return {
                    "priority": "critical",
                    "score": 92,
                    "geo_bumped": False,
                    "reasoning": "Dry-run: депег крупного стейблкоина.",
                }
            if "предсказывают рост" in prompt or '"topics": []' in prompt:
                return {
                    "priority": "low",
                    "score": 18,
                    "geo_bumped": False,
                    "reasoning": "Dry-run: низкая значимость.",
                }
            if "нбрк" in prompt or "geo_priority\": 1" in prompt:
                return {
                    "priority": "high",
                    "score": 78,
                    "geo_bumped": True,
                    "reasoning": "Dry-run: казахстанский CBDC получает geo-bump.",
                }
            return {
                "priority": "medium",
                "score": 50,
                "geo_bumped": False,
                "reasoning": "Dry-run оценка значимости.",
            }
        if skill_name == "crypto-news-deduplicator":
            if "art_001" in prompt and "art_002" in prompt:
                return {
                    "clusters": [
                        {
                            "cluster_id": "c1",
                            "canonical_id": "art_001",
                            "member_ids": ["art_001", "art_002"],
                            "event_summary": "Пилот цифрового тенге",
                            "rationale": "Dry-run cluster.",
                        }
                    ],
                    "singletons": ["art_003"],
                }
            ids = re.findall(r'"id":\s*"(art_[0-9]+)"', user_prompt)
            return {"clusters": [], "singletons": ids}
        if skill_name == "crypto-digest-builder":
            if "art_001" in prompt:
                html = (
                    "<html><body><h1>12 марта 2025</h1>"
                    "<h2>Регулирование РК</h2>nationalbank.kz "
                    "Halyk Bank <h2>Биржи</h2>Bittrex</body></html>"
                )
                return {
                    "digest_date": "2025-03-12",
                    "html": html,
                    "plain_text": "12 марта 2025\nnationalbank.kz\nHalyk Bank\nBittrex",
                    "telegram_segments": ["12\\.03 nationalbank\\.kz Halyk Bank Bittrex"],
                    "stats": {"total_articles": 3},
                }
            if "bitcoin" in prompt.lower():
                return {
                    "digest_date": "2025-03-13",
                    "html": "<html><body>Bitcoin превысил $100k</body></html>",
                    "plain_text": "Bitcoin превысил $100k",
                    "telegram_segments": ["Bitcoin превысил $100k"],
                    "stats": {"total_articles": 1},
                }
            return {
                "digest_date": "dry-run",
                "html": "<html><body><h1>Dry-run digest</h1></body></html>",
                "plain_text": "Dry-run digest",
                "telegram_segments": ["Dry-run digest"],
                "stats": {"total_articles": 0},
            }
        if skill_name == "crypto-digest-quality-check":
            if "securities and exchange commission today announced charges" in prompt:
                return {
                    "passed": False,
                    "severity": "blocker",
                    "issues": [{"category": "copyright", "severity": "blocker"}],
                    "warnings": [],
                    "recommendation": "do_not_send",
                }
            if "шокирующее" in prompt or "невероятный успех" in prompt:
                return {
                    "passed": True,
                    "severity": "major",
                    "issues": [{"category": "tone", "severity": "major"}],
                    "warnings": [],
                    "recommendation": "send_with_caution",
                }
            return {
                "passed": True,
                "severity": "none",
                "issues": [],
                "warnings": [],
                "recommendation": "send",
            }
        if skill_name == "crypto-source-validator":
            if "anon_crypto_alpha" in prompt:
                return {
                    "recommended": False,
                    "score": 12,
                    "confidence": 0.95,
                    "pros": [],
                    "cons": ["Anonymity, sensationalism and speculation."],
                    "technical_notes": "Telegram source.",
                    "suggested_priority": None,
                    "suggested_topics_focus": [],
                }
            if "afsa.aifc.kz" in prompt:
                return {
                    "recommended": True,
                    "score": 78,
                    "confidence": 0.9,
                    "pros": ["Регулятор Казахстана."],
                    "cons": ["Нет RSS."],
                    "technical_notes": "HTML parser required.",
                    "suggested_priority": 1,
                    "suggested_topics_focus": ["regulation", "licensing"],
                }
            if "coindesk.com" in prompt:
                return {
                    "recommended": True,
                    "score": 82,
                    "confidence": 0.9,
                    "pros": ["RSS available; high frequency of publication."],
                    "cons": ["Требуется фильтрация шума."],
                    "technical_notes": "RSS feed available.",
                    "suggested_priority": 3,
                    "suggested_topics_focus": ["regulation", "exchanges"],
                }
            return {
                "recommended": False,
                "score": 35,
                "confidence": 0.4,
                "pros": [],
                "cons": ["missing sample data; insufficient information."],
                "technical_notes": "Dry-run режим. Требуется ручной sample.",
                "suggested_priority": 3,
                "suggested_topics_focus": [],
            }
        return {"ok": True, "mode": "dry-run"}

    @staticmethod
    def _detect_skill(system_prompt: str) -> str | None:
        for line in system_prompt.splitlines():
            normalized = line.strip().lower()
            if normalized.startswith("name: "):
                return normalized.removeprefix("name: ").strip()
        return None


_TRANSIENT_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def _is_transient_error(exc: Exception) -> bool:
    """Decide whether a Gemini SDK error is worth retrying.

    Covers rate limits (429), transient server errors (5xx), and network
    timeouts. Client errors like 400/401/403 are not retried — they will
    fail again identically and only waste the budget.
    """

    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_STATUS_CODES:
        return True
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "servererror", "unavailable", "connect")):
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in ("rate limit", "resource_exhausted", "timeout", "temporarily", "503", "429")
    )


def _window_around_id(text: str, article_id: str, radius: int = 900) -> str:
    position = text.find(article_id)
    if position == -1:
        return text[:radius]
    start = max(0, position - radius // 2)
    end = min(len(text), position + radius)
    return text[start:end]


def _extract_event_scale(window: str) -> str | None:
    match = re.search(r'"event_scale":\s*"([a-z_]+)"', window)
    if not match:
        return None
    value = match.group(1)
    if value in {"kz_major", "cis_major", "global_major", "minor"}:
        return value
    return None
