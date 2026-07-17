from datetime import datetime, timedelta, timezone

from shared.chat_log import append_chat_message, format_for_prompt, messages_since


def test_append_and_messages_since(tmp_path):
    path = str(tmp_path / "chat.json")
    append_chat_message(path, author="Кирилл", text="давайте сделаем ретро")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    messages = messages_since(path, since)

    assert len(messages) == 1
    assert messages[0]["author"] == "Кирилл"
    assert messages[0]["text"] == "давайте сделаем ретро"


def test_messages_since_excludes_older_than_cutoff(tmp_path):
    path = str(tmp_path / "chat.json")
    append_chat_message(path, author="Аня", text="старое сообщение")

    future_cutoff = datetime.now(timezone.utc) + timedelta(hours=1)
    assert messages_since(path, future_cutoff) == []


def test_messages_since_missing_file_returns_empty(tmp_path):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert messages_since(str(tmp_path / "missing.json"), since) == []


def test_append_caps_at_max_entries(tmp_path):
    path = str(tmp_path / "chat.json")
    for i in range(410):
        append_chat_message(path, author="Кто-то", text=f"сообщение {i}")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    messages = messages_since(path, since)
    assert len(messages) == 400
    assert messages[0]["text"] == "сообщение 10"
    assert messages[-1]["text"] == "сообщение 409"


def test_format_for_prompt_joins_author_and_text():
    messages = [{"author": "Кирилл", "text": "первое"}, {"author": "Аня", "text": "второе"}]
    formatted = format_for_prompt(messages)
    assert formatted == "Кирилл: первое\nАня: второе"


def test_format_for_prompt_truncates_keeping_recent_tail():
    messages = [{"author": "A", "text": "старое" * 100}, {"author": "B", "text": "новое"}]
    formatted = format_for_prompt(messages, max_chars=20)
    assert formatted.endswith("B: новое")
    assert len(formatted) == 20
