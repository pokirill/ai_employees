from channel_bot.post_history import load_recent_summaries, record_published_post


def test_record_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "history.json")

    record_published_post(path, category="feature", summary="Пост про цели", published_at="2026-07-17T10:00:00+00:00")
    record_published_post(path, category="poll", summary="Опрос про подписки", published_at="2026-07-17T14:30:00+00:00")

    assert load_recent_summaries(path) == ["Пост про цели", "Опрос про подписки"]


def test_load_recent_summaries_missing_file_returns_empty(tmp_path):
    assert load_recent_summaries(str(tmp_path / "missing.json")) == []


def test_load_recent_summaries_respects_limit(tmp_path):
    path = str(tmp_path / "history.json")
    for i in range(5):
        record_published_post(path, category="feature", summary=f"пост {i}", published_at="2026-07-17T10:00:00+00:00")

    assert load_recent_summaries(path, limit=2) == ["пост 3", "пост 4"]


def test_record_published_post_caps_at_max_entries(tmp_path):
    path = str(tmp_path / "history.json")
    for i in range(20):
        record_published_post(path, category="feature", summary=f"пост {i}", published_at="2026-07-17T10:00:00+00:00")

    summaries = load_recent_summaries(path, limit=100)
    assert len(summaries) == 15
    assert summaries[0] == "пост 5"
    assert summaries[-1] == "пост 19"


def test_load_recent_summaries_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("не json", encoding="utf-8")

    assert load_recent_summaries(str(path)) == []
