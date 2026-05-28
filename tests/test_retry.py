import pytest

from crypto_monitor.retry import retry_call


def test_retry_call_retries_until_success() -> None:
    attempts = {"count": 0}

    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise OSError("temporary")
        return "ok"

    assert retry_call(flaky, attempts=2, base_delay_seconds=0) == "ok"
    assert attempts["count"] == 2


def test_retry_call_raises_last_error() -> None:
    with pytest.raises(OSError):
        retry_call(lambda: (_ for _ in ()).throw(OSError("nope")), attempts=2, base_delay_seconds=0)
