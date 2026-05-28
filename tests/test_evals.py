from crypto_monitor.evals import check_assertion


def test_check_assertion_field_present() -> None:
    assert check_assertion({"priority": "high"}, "priority is present") is None
    assert check_assertion({}, "priority is present") == "missing field priority"


def test_check_assertion_contains_text() -> None:
    output = {"summary": "Национальный банк РК запустил проект"}
    assert check_assertion(output, "summary contains 'банк РК'") is None


def test_check_assertion_telegram_length() -> None:
    output = {"telegram_segments": ["short"]}
    assert check_assertion(output, "all telegram_segments items have length <= 4000") is None


def test_check_assertion_does_not_contain() -> None:
    assert (
        check_assertion({"summary": "clean text"}, "summary does not contain 'forbidden'")
        is None
    )
    assert check_assertion({"summary": "forbidden text"}, "summary does not contain 'forbidden'")
