from channel_bot.feedback_store import add_feedback, load_feedback, remove_feedback


def test_add_feedback_appends(tmp_path):
    path = str(tmp_path / "feedback.json")

    add_feedback(path, "первое замечание")
    add_feedback(path, "второе замечание")

    assert load_feedback(path) == ["первое замечание", "второе замечание"]


def test_load_feedback_missing_file_returns_empty(tmp_path):
    assert load_feedback(str(tmp_path / "missing.json")) == []


def test_load_feedback_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "feedback.json"
    path.write_text("не json", encoding="utf-8")

    assert load_feedback(str(path)) == []


def test_add_feedback_caps_at_max_entries_evicting_oldest(tmp_path):
    path = str(tmp_path / "feedback.json")

    for i in range(15):
        add_feedback(path, f"замечание {i}")

    items = load_feedback(path)
    assert len(items) == 12
    assert items[0] == "замечание 3"
    assert items[-1] == "замечание 14"


def test_remove_feedback_by_index(tmp_path):
    path = str(tmp_path / "feedback.json")
    add_feedback(path, "a")
    add_feedback(path, "b")
    add_feedback(path, "c")

    removed = remove_feedback(path, 1)

    assert removed == "b"
    assert load_feedback(path) == ["a", "c"]


def test_remove_feedback_out_of_range_returns_none(tmp_path):
    path = str(tmp_path / "feedback.json")
    add_feedback(path, "a")

    assert remove_feedback(path, 5) is None
    assert load_feedback(path) == ["a"]
