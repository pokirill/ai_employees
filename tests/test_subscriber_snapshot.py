from datetime import datetime, timezone

from channel_bot.subscriber_snapshot import load_subscriber_snapshot, save_subscriber_snapshot


def test_save_and_load_roundtrip(tmp_path):
    state_file = tmp_path / "snapshot.json"
    now = datetime.now(timezone.utc)

    save_subscriber_snapshot(str(state_file), 123, now)
    loaded = load_subscriber_snapshot(str(state_file))

    assert loaded is not None
    assert loaded["count"] == 123
    assert abs((loaded["recorded_at"] - now).total_seconds()) < 1


def test_load_missing_file_returns_none(tmp_path):
    assert load_subscriber_snapshot(str(tmp_path / "missing.json")) is None


def test_load_corrupted_file_returns_none(tmp_path):
    state_file = tmp_path / "snapshot.json"
    state_file.write_text("not json at all", encoding="utf-8")

    assert load_subscriber_snapshot(str(state_file)) is None
