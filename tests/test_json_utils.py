import pytest

from crypto_monitor.json_utils import JsonExtractionError, extract_json_object


def test_extract_plain_json() -> None:
    assert extract_json_object('{"ok": true}') == {"ok": True}


def test_extract_fenced_json() -> None:
    assert extract_json_object('```json\n{"value": 3}\n```') == {"value": 3}


def test_extract_rejects_arrays() -> None:
    with pytest.raises(JsonExtractionError):
        extract_json_object("[1, 2, 3]")
