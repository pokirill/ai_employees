from datetime import datetime, timezone

from channel_bot.post_state import load_last_post_at, load_last_post_info, save_last_post_at


def test_save_and_load_last_post_at_roundtrip(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime.now(timezone.utc)

    save_last_post_at(str(state_file), now)
    loaded = load_last_post_at(str(state_file))

    assert loaded is not None
    assert abs((loaded - now).total_seconds()) < 1


def test_load_last_post_at_missing_file_returns_none(tmp_path):
    assert load_last_post_at(str(tmp_path / "missing.json")) is None


def test_save_and_load_last_post_info_includes_title(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime.now(timezone.utc)

    save_last_post_at(str(state_file), now, title="Заголовок поста")
    info = load_last_post_info(str(state_file))

    assert info is not None
    assert info["last_post_title"] == "Заголовок поста"
    assert abs((info["last_post_at"] - now).total_seconds()) < 1


def test_load_last_post_info_missing_file_returns_none(tmp_path):
    assert load_last_post_info(str(tmp_path / "missing.json")) is None


def test_load_last_post_info_defaults_title_when_absent(tmp_path):
    # Обратная совместимость: файл, записанный до появления title (старым
    # save_last_post_at без параметра), не должен ломать load_last_post_info.
    state_file = tmp_path / "state.json"
    state_file.write_text('{"last_post_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")

    info = load_last_post_info(str(state_file))

    assert info is not None
    assert info["last_post_title"] == ""
