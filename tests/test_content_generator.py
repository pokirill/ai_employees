from __future__ import annotations

import pytest

from channel_bot.content_generator import (
    _chat_within_length_limit,
    _collect_release_notes_entries,
    generate_clarifying_questions,
    generate_compose_post,
    generate_feedback_metrics_post,
    generate_next_post,
    generate_release_notes_post,
    revise_post,
)
from channel_bot.content_generator import GeneratedPost
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


def test_chat_within_length_limit_returns_first_response_when_short_enough():
    llm = _FakeLLM(responses=["короткий текст"])

    text = _chat_within_length_limit(llm, "system", "user", max_tokens=100, limit_chars=350)

    assert text == "короткий текст"
    assert len(llm.calls) == 1


def test_chat_within_length_limit_retries_once_when_too_long():
    long_text = "а" * 400
    short_text = "а" * 200
    llm = _FakeLLM(responses=[long_text, short_text])

    text = _chat_within_length_limit(llm, "system", "user", max_tokens=100, limit_chars=350)

    assert text == short_text
    assert len(llm.calls) == 2


def test_chat_within_length_limit_gives_up_after_max_attempts():
    # Единственный элемент — _FakeLLM возвращает его на каждый вызов без
    # изменений (см. её .chat()), имитируя модель, которая не может
    # уложиться в лимит даже после подсказок сократить.
    long_text = "а" * 400
    llm = _FakeLLM(responses=[long_text])

    text = _chat_within_length_limit(llm, "system", "user", max_tokens=100, limit_chars=350)

    assert text == long_text  # не блокируем публикацию — админ увидит длинный черновик на ревью
    assert len(llm.calls) == 3  # 1 первая попытка + 2 повтора (_LENGTH_RETRY_ATTEMPTS)


def _write_changelog(path, *titles_and_bodies):
    lines = []
    for title, body in titles_and_bodies:
        lines.append(f"- **[{title}]**\n{body}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_collect_release_notes_entries_gathers_multiple_up_to_max(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    _write_changelog(
        changelog_path,
        ("Фича А", "Описание А"),
        ("Фича Б", "Описание Б"),
        ("Фича В", "Описание В"),
    )
    used_state_path = str(tmp_path / "used.json")

    entries = _collect_release_notes_entries(_FakeLLM(), str(changelog_path), used_state_path, dry_run=False, max_items=2)

    assert [e["title"] for e in entries] == ["Фича А", "Фича Б"]


def test_collect_release_notes_entries_leaves_overflow_for_next_week(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    _write_changelog(changelog_path, ("Фича А", "А"), ("Фича Б", "Б"), ("Фича В", "В"))
    used_state_path = str(tmp_path / "used.json")

    _collect_release_notes_entries(_FakeLLM(), str(changelog_path), used_state_path, dry_run=False, max_items=2)
    second_week = _collect_release_notes_entries(_FakeLLM(), str(changelog_path), used_state_path, dry_run=False, max_items=2)

    assert [e["title"] for e in second_week] == ["Фича В"]


def test_collect_release_notes_entries_dry_run_does_not_mark_used(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    _write_changelog(changelog_path, ("Фича А", "А"), ("Фича Б", "Б"))
    used_state_path = str(tmp_path / "used.json")

    _collect_release_notes_entries(_FakeLLM(), str(changelog_path), used_state_path, dry_run=True, max_items=5)

    from channel_bot.changelog_entries import load_used_titles

    assert load_used_titles(used_state_path) == set()


def test_generate_release_notes_post_returns_none_when_no_entries(tmp_path):
    used_state_path = str(tmp_path / "used.json")

    post = generate_release_notes_post(_FakeLLM(), str(tmp_path / "missing_changelog.md"), used_state_path)

    assert post is None


def test_generate_release_notes_post_includes_all_collected_entries_in_prompt(tmp_path):
    changelog_path = tmp_path / "AI_CHANGELOG.md"
    _write_changelog(changelog_path, ("Фича А", "Описание А"), ("Фича Б", "Описание Б"))
    used_state_path = str(tmp_path / "used.json")
    llm = _FakeLLM()

    post = generate_release_notes_post(llm, str(changelog_path), used_state_path)

    assert post is not None
    assert post.kind == "text"
    user_content = llm.calls[-1][1]["content"]
    assert "Фича А" in user_content
    assert "Фича Б" in user_content


def test_generate_feedback_metrics_post_mentions_delta_when_given():
    llm = _FakeLLM()

    generate_feedback_metrics_post(llm, 150, 12)

    user_content = llm.calls[-1][1]["content"]
    assert "150" in user_content
    assert "+12" in user_content


def test_generate_feedback_metrics_post_omits_delta_when_none():
    llm = _FakeLLM()

    generate_feedback_metrics_post(llm, 150, None)

    user_content = llm.calls[-1][1]["content"]
    assert "150" in user_content
    assert "нет" in user_content.lower()


def test_generate_clarifying_questions_returns_empty_when_ready():
    llm = _FakeLLM(responses=["ГОТОВО"])

    questions = generate_clarifying_questions(llm, "icon_story", qa_pairs=[])

    assert questions == []


def test_generate_clarifying_questions_parses_and_caps_at_four():
    raw = "\n".join(f"ВОПРОС: Вопрос {i}?" for i in range(6))
    llm = _FakeLLM(responses=[raw])

    questions = generate_clarifying_questions(llm, "team_roster", qa_pairs=[])

    assert len(questions) == 4
    assert questions[0] == "Вопрос 0?"


def test_generate_clarifying_questions_fails_open_on_unparseable_response():
    llm = _FakeLLM(responses=["что-то невнятное, не тот формат"])

    questions = generate_clarifying_questions(llm, "icon_story", qa_pairs=[])

    assert questions == []


def test_generate_clarifying_questions_includes_prior_qa_in_prompt():
    llm = _FakeLLM(responses=["ГОТОВО"])
    qa_pairs = [("Кто предложил идею?", "Дизайнер Аня")]

    generate_clarifying_questions(llm, "icon_story", qa_pairs=qa_pairs)

    user_content = llm.calls[-1][1]["content"]
    assert "Дизайнер Аня" in user_content


def test_generate_compose_post_icon_story_includes_facts_and_respects_own_length_limit():
    llm = _FakeLLM(responses=["а" * 500])  # >350 (обычный лимит), но <700 (лимит icon_story)
    qa_pairs = [("Сколько было вариантов?", "Три варианта, один шуточный")]

    post = generate_compose_post(llm, "icon_story", qa_pairs)

    assert post.kind == "text"
    assert len(llm.calls) == 1  # 500 < 700 — retry не нужен
    user_content = llm.calls[-1][1]["content"]
    assert "Три варианта, один шуточный" in user_content


def test_generate_compose_post_team_roster_respects_larger_length_limit():
    llm = _FakeLLM(responses=["а" * 1000])  # >700, но <1500 (лимит team_roster)
    qa_pairs = [("Кто в команде?", "Аня — дизайн, Кирилл — продукт")]

    post = generate_compose_post(llm, "team_roster", qa_pairs)

    assert post.kind == "text"
    assert len(llm.calls) == 1


def test_revise_post_icon_story_uses_own_length_limit_not_regular_350():
    # Регрессия того же класса, что уже дважды ловили для "intro" (см.
    # _COMPOSE_POST_SPECS в content_generator.py): без явной ветки для
    # icon_story/team_roster правка отката бы черновик к обычным 350
    # символам, здесь 500 символов не должно триггерить повтор.
    llm = _FakeLLM(responses=["а" * 500])
    original = GeneratedPost(kind="text", text="старый черновик")

    revise_post(llm, original, "сделай теплее", category="icon_story")

    assert len(llm.calls) == 1


def test_revise_post_default_category_still_uses_regular_350_limit():
    llm = _FakeLLM(responses=["а" * 500, "а" * 200])
    original = GeneratedPost(kind="text", text="старый черновик")

    revise_post(llm, original, "сделай теплее", category=None)

    assert len(llm.calls) == 2  # 500 > 350 — должен был сработать retry


def test_generate_clarifying_questions_includes_chat_context_when_given():
    llm = _FakeLLM(responses=["ГОТОВО"])

    generate_clarifying_questions(llm, "team_roster", qa_pairs=[], chat_context="Кирилл: у нас в команде теперь и Паша на бэкенде")

    user_content = llm.calls[-1][1]["content"]
    assert "Паша на бэкенде" in user_content


def test_generate_clarifying_questions_omits_chat_context_block_when_empty():
    llm = _FakeLLM(responses=["ГОТОВО"])

    generate_clarifying_questions(llm, "icon_story", qa_pairs=[], chat_context="")

    user_content = llm.calls[-1][1]["content"]
    assert "Выдержка из чата команды" not in user_content


def test_generate_compose_post_includes_chat_context_alongside_qa():
    llm = _FakeLLM(responses=["готовый пост"])

    generate_compose_post(llm, "icon_story", qa_pairs=[("Кто предложил?", "Аня")], chat_context="Аня: гляньте новый вариант иконки")

    user_content = llm.calls[-1][1]["content"]
    assert "Аня: гляньте новый вариант иконки" in user_content
    assert "Аня" in user_content
