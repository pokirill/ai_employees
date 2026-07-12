from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """R-COST: защита бюджета от одного болтливого чата/пользователя —
    не более `max_calls` LLM-вызовов за `window_seconds` на ключ (обычно
    chat_id). Полностью в памяти — при рестарте бота лимит обнуляется, это
    ок (не защита от злоумышленника, а страховка от случайного спама)."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: dict[int | str, deque[float]] = defaultdict(deque)

    def allow(self, key: int | str, now: float | None = None) -> bool:
        if self.max_calls <= 0:
            return True  # 0/отрицательное значение — лимит выключен
        now = now if now is not None else time.monotonic()
        timestamps = self._calls[key]
        cutoff = now - self.window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if len(timestamps) >= self.max_calls:
            return False
        timestamps.append(now)
        return True

    def seconds_until_available(self, key: int | str, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        timestamps = self._calls[key]
        if not timestamps or len(timestamps) < self.max_calls:
            return 0.0
        return max(0.0, self.window_seconds - (now - timestamps[0]))
