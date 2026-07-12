from __future__ import annotations

from shared.rate_limiter import SlidingWindowLimiter


def test_allows_up_to_max_calls():
    limiter = SlidingWindowLimiter(max_calls=3, window_seconds=60)
    assert limiter.allow("chat1", now=0) is True
    assert limiter.allow("chat1", now=1) is True
    assert limiter.allow("chat1", now=2) is True
    assert limiter.allow("chat1", now=3) is False


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(max_calls=1, window_seconds=60)
    assert limiter.allow("chat1", now=0) is True
    assert limiter.allow("chat2", now=0) is True
    assert limiter.allow("chat1", now=1) is False


def test_window_slides_and_frees_up_slots():
    limiter = SlidingWindowLimiter(max_calls=1, window_seconds=60)
    assert limiter.allow("chat1", now=0) is True
    assert limiter.allow("chat1", now=30) is False
    assert limiter.allow("chat1", now=61) is True


def test_zero_max_calls_disables_limit():
    limiter = SlidingWindowLimiter(max_calls=0, window_seconds=60)
    for i in range(100):
        assert limiter.allow("chat1", now=i) is True


def test_seconds_until_available_reports_zero_when_room_remains():
    limiter = SlidingWindowLimiter(max_calls=2, window_seconds=60)
    limiter.allow("chat1", now=0)
    assert limiter.seconds_until_available("chat1", now=1) == 0.0


def test_seconds_until_available_reports_remaining_wait():
    limiter = SlidingWindowLimiter(max_calls=1, window_seconds=60)
    limiter.allow("chat1", now=0)
    assert limiter.seconds_until_available("chat1", now=10) == 50.0
