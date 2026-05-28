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
) -> T:
    """Run a callable with simple exponential backoff."""

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
            time.sleep(base_delay_seconds * (2 ** (attempt - 1)))

    assert last_error is not None
    raise last_error
