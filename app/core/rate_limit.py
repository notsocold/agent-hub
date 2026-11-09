"""进程内滑动窗口限流。

M1 单实例部署够用；多实例时每个实例各自计数（M6 再换 Redis）。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.core.config import get_settings
from app.core.errors import RateLimitedError


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        if not get_settings().rate_limit_enabled:
            return
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now))
                raise RateLimitedError(
                    f"操作过于频繁，请在 {retry_after} 秒后重试",
                    details={"retry_after_seconds": retry_after},
                )
            bucket.append(now)


limiter = SlidingWindowLimiter()
