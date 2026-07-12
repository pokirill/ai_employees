from datetime import datetime, timedelta, timezone

from channel_bot.post_state import load_last_post_at, save_last_post_at, seconds_until_next_post


def test_seconds_until_next_post_no_state_returns_zero(tmp_path):
    state_file = tmp_path / "state.json"

    assert seconds_until_next_post(str(state_file), interval_hours=24) == 0.0


def test_save_and_load_last_post_at_roundtrip(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime.now(timezone.utc)

    save_last_post_at(str(state_file), now)
    loaded = load_last_post_at(str(state_file))

    assert loaded is not None
    assert abs((loaded - now).total_seconds()) < 1


def test_seconds_until_next_post_recent_post_returns_remaining_time(tmp_path):
    state_file = tmp_path / "state.json"
    posted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    save_last_post_at(str(state_file), posted_at)

    remaining = seconds_until_next_post(str(state_file), interval_hours=24)

    assert 22 * 3600 < remaining < 24 * 3600


def test_seconds_until_next_post_overdue_returns_zero(tmp_path):
    state_file = tmp_path / "state.json"
    posted_at = datetime.now(timezone.utc) - timedelta(hours=48)
    save_last_post_at(str(state_file), posted_at)

    assert seconds_until_next_post(str(state_file), interval_hours=24) == 0.0


def test_load_last_post_at_missing_file_returns_none(tmp_path):
    assert load_last_post_at(str(tmp_path / "missing.json")) is None
