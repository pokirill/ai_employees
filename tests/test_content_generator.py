from __future__ import annotations

import pytest

from channel_bot.content_generator import generate_next_post
from channel_bot.content_queue import load_queue, save_queue


class _FakeLLM:
    def __init__(self, responses=None, classifier_verdict="да"):
        self.calls = []
        self._responses = list(responses) if responses is not None else None
        self._classifier_verdict = classifier_verdict

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        system_content = messages[0]["content"] if messages else ""
        if "ПУБЛИЧНОГО поста" in system_content:  # маркер _TOPIC_CLASSIFIER_PROMPT
            return self._classifier_verdict
        if self._responses is not None:
            return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return "сгенерированный пост"


@pytest.fixture(autouse=True)
def _no_randomness_by_default(monkeypatch):
    # По умолчанию — никогда не опрос, никогда не приглашение в бету, чтобы
    # тесты не зависели от реального random.random() (иначе ~15% тестов
    # были бы флакующими из-за _POLL_PROBABILITY). Тесты, которым нужна
    # конкретная вероятность, переопределяют это внутри себя.
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.99)


def test_dry_run_does_not_pop_queue_topic(tmp_path):
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A", "Тема B"])
    llm = _FakeLLM()

    post = generate_next_post(
        llm,
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        dry_run=True,
    )

    assert post.kind == "text"
    assert post.text == "сгенерированный пост"
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


def test_internal_only_changelog_entry_is_skipped_as_topic(tmp_path):
    # Регрессия: пост про "стрик риск" (внутренняя механика удержания)
    # реально ушёл в канал — такие записи не должны становиться темой
    # поста вообще, не просто переформулироваться.
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    changelog_path.write_text(
        "- **[R-PUSH — стрик риск пуш]**\nВнутренняя механика антиоттока.\n"
        "- **[Публичная фича]**\nМожно смотреть цели по отдельности.\n",
        encoding="utf-8",
    )
    used_state_path = str(tmp_path / "used.json")
    llm = _FakeLLM()

    generate_next_post(
        llm,
        queue_path=str(tmp_path / "empty_queue.json"),
        changelog_path=str(changelog_path),
        used_state_path=used_state_path,
        docs_path=str(tmp_path),
    )

    assert "Публичная фича" in llm.calls[0][1]["content"]
    assert "стрик риск" not in llm.calls[0][1]["content"].lower()


def test_internal_only_entry_marked_used_so_it_never_resurfaces(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    changelog_path.write_text("- **[R-PUSH — стрик риск пуш]**\nВнутренняя механика.\n", encoding="utf-8")
    used_state_path = str(tmp_path / "used.json")

    generate_next_post(
        _FakeLLM(),
        queue_path=str(tmp_path / "empty_queue.json"),
        changelog_path=str(changelog_path),
        used_state_path=used_state_path,
        docs_path=str(tmp_path),
    )

    from channel_bot.changelog_entries import load_used_titles

    assert "R-PUSH — стрик риск пуш" in load_used_titles(used_state_path)


def test_internal_only_entry_not_marked_used_during_dry_run(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    changelog_path.write_text("- **[R-PUSH — стрик риск пуш]**\nВнутренняя механика.\n", encoding="utf-8")
    used_state_path = str(tmp_path / "used.json")

    generate_next_post(
        _FakeLLM(),
        queue_path=str(tmp_path / "empty_queue.json"),
        changelog_path=str(changelog_path),
        used_state_path=used_state_path,
        docs_path=str(tmp_path),
        dry_run=True,
    )

    from channel_bot.changelog_entries import load_used_titles

    assert load_used_titles(used_state_path) == set()


def test_beta_invite_appended_when_probability_hits(tmp_path, monkeypatch):
    # Первый вызов random.random() — проверка "опрос ли это" (промах, 0.99),
    # второй — проверка приглашения в бету (попадание, 0.01).
    values = iter([0.99, 0.01])
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: next(values))
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    post = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        beta_invite_url="https://example.com/beta",
    )

    assert post.kind == "text"
    assert "https://example.com/beta" in post.text


def test_beta_invite_not_appended_when_probability_misses(tmp_path):
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    post = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
        beta_invite_url="https://example.com/beta",
    )

    assert post.text == "сгенерированный пост"


def test_beta_invite_never_appended_without_url(tmp_path):
    # Даже если "повезло бы" с вероятностью — без URL приглашать некуда,
    # не выдумываем ссылку.
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])

    post = generate_next_post(
        _FakeLLM(),
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
    )

    assert post.text == "сгенерированный пост"


def test_poll_generated_when_probability_hits_and_format_valid(tmp_path, monkeypatch):
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.0)
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])
    llm = _FakeLLM(responses=["ВОПРОС: Как вы следите за тратами?\nВАРИАНТ: В приложении\nВАРИАНТ: На бумаге\nВАРИАНТ: Никак"])

    post = generate_next_post(
        llm,
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
    )

    assert post.kind == "poll"
    assert post.question == "Как вы следите за тратами?"
    assert post.options == ["В приложении", "На бумаге", "Никак"]


def test_poll_falls_back_to_text_when_format_unparseable(tmp_path, monkeypatch):
    monkeypatch.setattr("channel_bot.content_generator.random.random", lambda: 0.0)
    queue_path = str(tmp_path / "queue.json")
    save_queue(queue_path, ["Тема A"])
    # LLM не ответил строгим форматом — первый вызов (попытка опроса)
    # возвращает что-то непарсибельное, второй (fallback на текст) — обычный пост.
    llm = _FakeLLM(responses=["не тот формат вообще", "обычный текстовый пост"])

    post = generate_next_post(
        llm,
        queue_path=queue_path,
        changelog_path=str(tmp_path / "missing_changelog.md"),
        used_state_path=str(tmp_path / "used.json"),
        docs_path=str(tmp_path),
    )

    assert post.kind == "text"
    assert post.text == "обычный текстовый пост"
    assert len(llm.calls) == 2


def test_poll_parser_rejects_single_option():
    from channel_bot.content_generator import _parse_poll_response

    assert _parse_poll_response("ВОПРОС: Вопрос?\nВАРИАНТ: Один вариант") is None


def test_poll_parser_caps_at_ten_options():
    from channel_bot.content_generator import _parse_poll_response

    raw = "ВОПРОС: Вопрос?\n" + "\n".join(f"ВАРИАНТ: Опция {i}" for i in range(15))
    parsed = _parse_poll_response(raw)

    assert parsed is not None
    _, options = parsed
    assert len(options) == 10
