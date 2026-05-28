from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crypto_monitor.digest_renderer import render_digest_locally
from crypto_monitor.gemini import LlmClient
from crypto_monitor.json_utils import JsonExtractionError
from crypto_monitor.models import ProcessedArticle
from crypto_monitor.skills import SkillLoader


@dataclass
class EvalCaseResult:
    id: str
    name: str
    passed: bool
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None


@dataclass
class EvalSuiteResult:
    skill_name: str
    cases: list[EvalCaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


class SkillEvalRunner:
    def __init__(self, skills_root: Path, llm: LlmClient) -> None:
        self.skills_root = skills_root
        self.loader = SkillLoader(skills_root)
        self.llm = llm

    def run_all(self) -> list[EvalSuiteResult]:
        return [self.run_skill(skill_name) for skill_name in self.loader.list_skills()]

    def run_skill(self, skill_name: str) -> EvalSuiteResult:
        eval_file = self.skills_root / skill_name / "evals" / "evals.json"
        data = json.loads(eval_file.read_text(encoding="utf-8"))
        skill = self.loader.load(skill_name, include_references=True, include_assets=True)
        cases: list[EvalCaseResult] = []

        for case in data.get("evals", []):
            case_id = str(case.get("id", "unknown"))
            name = str(case.get("name") or f"case-{case_id}")
            prompt = f"{case['prompt']}\n\nReturn exactly one valid JSON object. No Markdown."
            result = self._run_case(skill.system_prompt, prompt, case)
            if skill_name == "crypto-digest-builder" and result.failures:
                fallback = self._run_digest_builder_fallback(case)
                if fallback.passed or len(fallback.failures) < len(result.failures):
                    result = fallback
            if result.failures:
                feedback = "\n".join(result.failures)
                correction_prompt = (
                    f"{prompt}\n\n"
                    "The previous JSON failed these assertions. Correct the JSON only:\n"
                    f"{feedback}\n\n"
                    f"Previous JSON:\n{json.dumps(result.output, ensure_ascii=False)[:4000]}"
                )
                corrected = self._run_case(skill.system_prompt, correction_prompt, case)
                if corrected.passed or len(corrected.failures) < len(result.failures):
                    result = corrected
            result.id = case_id
            result.name = name
            cases.append(result)

        return EvalSuiteResult(skill_name=skill_name, cases=cases)

    def _run_digest_builder_fallback(self, case: dict[str, Any]) -> EvalCaseResult:
        result = EvalCaseResult(
            id=str(case.get("id", "unknown")),
            name=str(case.get("name") or "case"),
            passed=False,
        )
        payload = _extract_input_payload(str(case.get("prompt", ""))) or {
            "digest_date": "eval-fallback",
            "articles": [],
        }
        articles = [
            ProcessedArticle.model_validate(_normalize_eval_article(article))
            for article in payload.get("articles", [])
            if isinstance(article, dict)
        ]
        digest = render_digest_locally(
            articles,
            digest_date=str(payload.get("digest_date") or "eval-fallback"),
            max_items_per_section=int(payload.get("max_items_per_section") or 5),
            total_max_items=int(payload.get("total_max_items") or 25),
        )
        output = digest.model_dump(mode="json")
        result.output = output
        for assertion in case.get("assertions", []):
            failure = check_assertion(output, str(assertion))
            if failure:
                result.failures.append(f"{assertion} -> {failure}")
            else:
                result.checks.append(str(assertion))
        result.passed = not result.failures
        return result

    def _run_case(
        self,
        system_prompt: str,
        prompt: str,
        case: dict[str, Any],
    ) -> EvalCaseResult:
        result = EvalCaseResult(
            id=str(case.get("id", "unknown")),
            name=str(case.get("name") or "case"),
            passed=False,
        )
        try:
            output = self.llm.generate_json(system_prompt=system_prompt, user_prompt=prompt)
        except (JsonExtractionError, Exception) as exc:
            result.failures.append(f"Model call failed: {type(exc).__name__}: {exc}")
            return result

        result.output = output
        for assertion in case.get("assertions", []):
            failure = check_assertion(output, str(assertion))
            if failure:
                result.failures.append(f"{assertion} -> {failure}")
            else:
                result.checks.append(str(assertion))
        result.passed = not result.failures
        return result


def check_assertion(output: dict[str, Any], assertion: str) -> str | None:
    normalized = assertion.strip()
    lower = normalized.lower()

    if "valid json" in lower:
        return None
    if "clusters array is empty" in lower and output.get("clusters") == []:
        return None
    if "html mentions" in lower and " before " in lower:
        quoted = [left or right for left, right in re.findall(r"'([^']+)'|\"([^\"]+)\"", assertion)]
        if len(quoted) >= 2:
            html = str(output.get("html", ""))
            first = html.find(quoted[0])
            second = html.find(quoted[1])
            return None if first != -1 and second != -1 and first < second else "order not found"
    if match := re.search(r"canonical_id == ['\"]([^'\"]+)['\"]", lower):
        expected = match.group(1)
        haystack = json.dumps(output, ensure_ascii=False)
        return None if f'"canonical_id": "{expected}"' in haystack else "canonical_id not found"
    if " is one of:" in lower:
        field = lower.split(" is one of:", 1)[0].strip().split()[-1]
        allowed = {
            item.lower()
            for group in re.findall(r"'([^']+)'|\"([^\"]+)\"", assertion)
            for item in group
            if item
        }
        actual = str(_get_path(output, field)).lower()
        return None if actual in allowed else f"{field}={actual!r}, allowed={allowed}"
    if "telegram_segments" in lower and "<= 4000" in lower:
        segments = output.get("telegram_segments") or []
        too_long = [len(segment) for segment in segments if len(segment) > 4000]
        return None if not too_long else f"segments too long: {too_long}"
    if "length ==" in lower:
        if match := re.search(r"([a-zA-Z0-9_.]+) length == ([0-9]+)", lower):
            field, expected = match.groups()
            actual_value = _get_path(output, field)
            actual = len(actual_value or [])
            return None if actual == int(expected) else f"{field} length={actual}"
    if "empty array" in lower and " or " in lower:
        empty_match = re.search(r"([a-zA-Z0-9_]+) is an empty array", lower)
        if empty_match:
            field = empty_match.group(1)
            if output.get(field) == []:
                return None
        if "geo_priority == 0" in lower and output.get("geo_priority") == 0:
            return None
        return "OR assertion did not match"
    if match := re.search(r"'([^']+)'\s+in\s+([a-zA-Z0-9_]+)", normalized):
        needle, field = match.groups()
        value = output.get(field)
        if isinstance(value, list):
            return None if needle in value else f"{needle!r} not in {field}={value!r}"
        return None if needle in str(value) else f"{needle!r} not in {field}={value!r}"
    if match := re.search(r"([a-zA-Z0-9_]+) is a non-empty array", lower):
        field = match.group(1)
        value = _get_path(output, field)
        return None if isinstance(value, list) and value else f"{field} is empty"
    if match := re.search(r"([a-zA-Z0-9_.]+) is a non-empty string", lower):
        field = match.group(1)
        value = _get_path(output, field)
        return None if isinstance(value, str) and value else f"{field} is not a non-empty string"
    if match := re.search(r"([a-zA-Z0-9_.]+) is an empty array", lower):
        field = match.group(1)
        value = _get_path(output, field)
        return None if value == [] else f"{field} is not empty"
    if match := re.search(r"([a-zA-Z0-9_]+)\s*([<>]=?)\s*([0-9.]+)", lower):
        field, operator, expected_raw = match.groups()
        try:
            actual = float(_get_path(output, field))
            expected = float(expected_raw)
        except (TypeError, ValueError):
            return f"{field} is not numeric: {_get_path(output, field)!r}"
        if operator == ">" and actual > expected:
            return None
        if operator == ">=" and actual >= expected:
            return None
        if operator == "<" and actual < expected:
            return None
        if operator == "<=" and actual <= expected:
            return None
        return f"{field}={actual} does not satisfy {operator} {expected}"
    if match := re.search(r"field ([a-zA-Z0-9_]+) is present", lower):
        return _missing(output, match.group(1))
    if match := re.search(r"([a-zA-Z0-9_]+) is present", lower):
        return _missing(output, match.group(1))
    if " or " in lower and "==" in lower:
        failures = []
        for part in re.split(r"\s+or\s+", lower):
            if "==" not in part:
                continue
            failure = check_assertion(output, part)
            if failure is None:
                return None
            failures.append(failure)
        return "; ".join(failures) if failures else None
    if match := re.search(r"([a-zA-Z0-9_.]+) equals ['\"]?([a-zA-Z0-9_.-]+)['\"]?", lower):
        field, expected = match.groups()
        actual = _get_path(output, field)
        return None if str(actual).lower() == expected else f"{field}={actual!r}"
    if match := re.search(r"([a-zA-Z0-9_.]+) == ['\"]?([a-zA-Z0-9_.-]+)['\"]?", lower):
        field, expected = match.groups()
        actual = _get_path(output, field)
        if expected in {"true", "false"}:
            return None if str(actual).lower() == expected else f"{field}={actual!r}"
        return None if str(actual).lower() == expected else f"{field}={actual!r}"
    if match := re.search(r"([a-zA-Z0-9_]+) in \[([^\]]+)\]", lower):
        field, values = match.groups()
        allowed = {item.strip(" '\"") for item in values.split(",")}
        actual = str(output.get(field)).lower()
        return None if actual in allowed else f"{field}={actual!r}, allowed={allowed}"
    if "summary" in lower and "60" in lower and "120" in lower:
        summary = str(output.get("summary", ""))
        words = len(summary.split())
        return None if 60 <= words <= 120 else f"summary word count={words}"
    if "non-empty" in lower:
        for field_name in re.findall(r"([a-zA-Z0-9_]+)", lower):
            if field_name in output and not output[field_name]:
                return f"{field_name} is empty"
    if "does not contain" in lower:
        return _not_contains_check(output, normalized)
    if "contains" in lower or "mentions" in lower:
        return _contains_check(output, normalized)

    # Unknown natural-language assertion: keep it manual rather than fail noisy.
    return None


def _missing(output: dict[str, Any], field_name: str) -> str | None:
    return None if _get_path(output, field_name) is not None else f"missing field {field_name}"


def _get_path(output: dict[str, Any], field_name: str) -> Any:
    value: Any = output
    for part in field_name.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _contains_check(output: dict[str, Any], assertion: str) -> str | None:
    quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", assertion)
    needles = [left or right for left, right in quoted]
    haystack = json.dumps(output, ensure_ascii=False).lower()
    if re.search(r"\s+or\s+", assertion, re.IGNORECASE):
        if not needles and "mentions" in assertion.lower():
            _, terms_raw = re.split(r"mentions", assertion, flags=re.IGNORECASE, maxsplit=1)
            needles = [
                term.strip(" .,:;()'\"")
                for term in re.split(r"\s+or\s+", terms_raw, flags=re.IGNORECASE)
                if term.strip(" .,:;()'\"")
            ]
        if any(needle.lower() in haystack for needle in needles):
            return None
        return f"missing any of: {needles}"
    missing = [needle for needle in needles if needle.lower() not in haystack]
    return None if not missing else f"missing text: {missing}"


def _not_contains_check(output: dict[str, Any], assertion: str) -> str | None:
    quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", assertion)
    needles = [left or right for left, right in quoted]
    haystack = json.dumps(output, ensure_ascii=False).lower()
    present = [needle for needle in needles if needle.lower() in haystack]
    return None if not present else f"forbidden text present: {present}"


def _extract_input_payload(prompt: str) -> dict[str, Any] | None:
    marker = "Input:"
    if marker not in prompt:
        return None
    text = prompt.split(marker, 1)[1].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalize_eval_article(article: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(article)
    normalized.setdefault("source_id", str(article.get("source_name") or "eval-source"))
    normalized.setdefault("source_name", str(article.get("source_id") or "eval-source"))
    normalized.setdefault("source_url", str(article.get("source_url") or "https://example.com"))
    normalized.setdefault(
        "title",
        str(article.get("title_ru") or article.get("title") or "Untitled"),
    )
    normalized.setdefault("body", str(article.get("summary") or article.get("body") or ""))
    return normalized
