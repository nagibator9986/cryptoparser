from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 1.0,
    retry_exceptions: tuple[type[Exception], ...] = (Exception,),
    delay_for_exception: Callable[[Exception], float | None] | None = None,
) -> T:
    """Run a callable with simple exponential backoff.

    ``delay_for_exception`` may inspect the raised exception and return an
    explicit delay (e.g. an HTTP ``Retry-After``) that overrides the default
    exponential backoff for that attempt.
    """

    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_exceptions as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = base_delay_seconds * (2 ** (attempt - 1))
            if delay_for_exception is not None:
                override = delay_for_exception(exc)
                if override is not None:
                    delay = override
            time.sleep(delay)

    assert last_error is not None
    raise last_error
