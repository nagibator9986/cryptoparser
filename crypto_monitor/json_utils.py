from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class JsonExtractionError(ValueError):
    """Raised when a model response cannot be parsed as JSON."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a single JSON object from model text.

    Gemini usually returns plain JSON in JSON mode, but this keeps the pipeline
    resilient during local testing or when a model wraps output in code fences.
    """

    cleaned = text.strip()
    match = _JSON_FENCE_RE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise JsonExtractionError("Response does not contain a JSON object") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise JsonExtractionError(f"Invalid JSON response: {exc}") from exc

    if not isinstance(value, dict):
        raise JsonExtractionError("Expected a JSON object")
    return value


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
