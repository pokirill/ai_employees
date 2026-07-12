from __future__ import annotations

from shared.context_heuristic import question_needs_project_context


def test_short_greeting_does_not_need_context():
    assert question_needs_project_context("привет") is False
    assert question_needs_project_context("спасибо!") is False


def test_keyword_triggers_context():
    assert question_needs_project_context("а баг в пуш-уведомлениях уже пофиксили?") is True
    assert question_needs_project_context("what's in the backlog for FinAssist?") is True


def test_keyword_match_is_case_insensitive():
    assert question_needs_project_context("АРХИТЕКТУРА бэкенда какая?") is True


def test_long_question_without_keywords_still_needs_context():
    long_question = "а можешь объяснить простыми словами почему так долго всё это тянется без ответа"
    assert len(long_question) >= 60
    assert question_needs_project_context(long_question) is True


def test_short_question_without_keywords_does_not_need_context():
    assert question_needs_project_context("как дела?") is False
