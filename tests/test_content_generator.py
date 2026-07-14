from __future__ import annotations

from channel_bot.content_generator import generate_next_post
from channel_bot.content_queue import load_queue, save_queue


class _FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return "сгенерированный пост"


def test_dry_run_does_not_pop_queue_topic(tmp_path):
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A", "Тема B"])
    llm = _FakeLLM()

    text = generate_next_post(
        llm,
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        dry_run=True,
    )

    assert text == "сгенерированный пост"
    assert load_queue(queue_path) == ["Тема A", "Тема B"]


def test_real_run_pops_queue_topic(tmp_path):
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A", "Тема B"])
    llm = _FakeLLM()

    generate_next_post(
        llm,
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
    )

    assert load_queue(queue_path) == ["Тема B"]


def test_dry_run_does_not_mark_changelog_title_used(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    changelog_path.write_text("- **[Тема из changelog]**\nТело записи.\n", encoding="utf-8")
    used_state_path = str(tmp_path / "used.json")
    llm = _FakeLLM()

    generate_next_post(
        llm,
        queue_path=str(tmp_path / "empty_queue.json"),
        changelog_path=str(changelog_path),
        used_state_path=used_state_path,
        docs_path=str(tmp_path),
        dry_run=True,
    )

    from channel_bot.changelog_entries import load_used_titles

    assert load_used_titles(used_state_path) == set()


def test_beta_invite_appended_when_probability_hits(tmp_path, monkeypatch):
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.01)
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    text = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        beta_invite_url="https://example.com/beta",
    )

    assert "https://example.com/beta" in text


def test_beta_invite_not_appended_when_probability_misses(tmp_path, monkeypatch):
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.99)
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    text = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        beta_invite_url="https://example.com/beta",
    )

    assert text == "сгенерированный пост"


def test_beta_invite_never_appended_without_url(tmp_path, monkeypatch):
    # Даже если "повезло бы" с вероятностью — без URL приглашать некуда,
    # не выдумываем ссылку.
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.0)
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    text = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
    )

    assert text == "сгенерированный пост"
