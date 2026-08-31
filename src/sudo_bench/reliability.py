import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sudo_bench.api import ApiError


@dataclass(frozen=True)
class ErrorInfo:
    category: str
    retryable: bool
    status_code: Optional[int] = None
    retry_after: Optional[float] = None


def classify_exception(exc: Exception) -> ErrorInfo:
    if isinstance(exc, ApiError):
        return ErrorInfo(
            category=exc.category,
            retryable=exc.retryable,
            status_code=exc.status_code,
            retry_after=exc.retry_after,
        )
    if isinstance(exc, TimeoutError):
        return ErrorInfo(category="timeout", retryable=True)
    if isinstance(exc, ConnectionError):
        return ErrorInfo(category="network_error", retryable=True)
    return ErrorInfo(category="internal_error", retryable=False)


def retry_delay(
    attempt: int,
    initial_seconds: float,
    maximum_seconds: float,
    retry_after: Optional[float],
) -> float:
    exponential = min(maximum_seconds, initial_seconds * (2 ** (attempt - 1)))
    return max(exponential, retry_after or 0.0)


class RateLimiter:
    def __init__(
        self,
        requests_per_second: Optional[float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def acquire(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self._interval
        if delay:
            self._sleep(delay)
