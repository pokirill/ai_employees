import json

from channel_bot.content_queue import append_topic, load_queue, peek_next_topic, pop_next_topic, save_queue


def test_pop_next_topic_returns_first_and_removes_it(tmp_path):
    queue_file = tmp_path / "queue.json"
    save_queue(str(queue_file), ["a", "b", "c"])

    popped = pop_next_topic(str(queue_file))

    assert popped == "a"
    assert load_queue(str(queue_file)) == ["b", "c"]


def test_pop_next_topic_empty_queue_returns_none(tmp_path):
    queue_file = tmp_path / "queue.json"
    save_queue(str(queue_file), [])

    assert pop_next_topic(str(queue_file)) is None


def test_pop_next_topic_missing_file_returns_none(tmp_path):
    queue_file = tmp_path / "does_not_exist.json"

    assert pop_next_topic(str(queue_file)) is None


def test_append_topic_adds_to_end(tmp_path):
    queue_file = tmp_path / "queue.json"
    save_queue(str(queue_file), ["a"])

    append_topic(str(queue_file), "b")

    assert load_queue(str(queue_file)) == ["a", "b"]


def test_peek_next_topic_returns_first_without_removing(tmp_path):
    queue_file = tmp_path / "queue.json"
    save_queue(str(queue_file), ["a", "b", "c"])

    peeked = peek_next_topic(str(queue_file))

    assert peeked == "a"
    assert load_queue(str(queue_file)) == ["a", "b", "c"]


def test_peek_next_topic_empty_queue_returns_none(tmp_path):
    queue_file = tmp_path / "queue.json"
    save_queue(str(queue_file), [])

    assert peek_next_topic(str(queue_file)) is None


def test_load_queue_malformed_json_returns_empty(tmp_path):
    queue_file = tmp_path / "queue.json"
    queue_file.write_text("не json вообще", encoding="utf-8")

    assert load_queue(str(queue_file)) == []
