from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"disregard\s+(all\s+)?previous\s+instructions",
        r"system\s+prompt",
        r"developer\s+message",
        r"you\s+are\s+now",
        r"forget\s+your\s+instructions",
        r"ранее\s+данн(ые|ых)\s+инструкц",
        r"игнорируй\s+инструкц",
        r"системн(ый|ого)\s+промпт",
    )
]


def sanitize_untrusted_text(value: str, max_chars: int = 30_000) -> tuple[str, list[str]]:
    """Sanitize source-controlled text before sending it to an LLM.

    The goal is not to censor news content. It is to make prompt-injection
    attempts visible and inert while preserving enough original context for
    classification and summarization.
    """

    warnings: list[str] = []
    text = value.replace("\x00", " ")

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            warnings.append(f"Potential prompt-injection phrase removed: {pattern.pattern}")
            text = pattern.sub("[removed prompt-injection phrase]", text)

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        warnings.append(f"Text truncated from {len(text)} to {max_chars} characters")
        text = text[:max_chars].rsplit(" ", 1)[0].strip()

    return text, warnings
