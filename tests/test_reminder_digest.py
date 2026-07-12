from __future__ import annotations

from shared.reminder_digest import build_reminder_digest
from shared.task_store import Task


def _task(**overrides) -> Task:
    defaults = dict(
        id=1,
        title="Задача",
        status="open",
        claimed_by=None,
        created_by="Аня",
        created_at="2026-07-12T10:00:00+00:00",
        completed_at=None,
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_no_pending_tasks_returns_none():
    assert build_reminder_digest([_task(status="done")]) is None
    assert build_reminder_digest([]) is None


def test_groups_by_claimer_with_mention_link():
    tasks = [_task(id=1, title="Купить домен", claimed_by="Кирилл", claimed_by_user_id=555)]
    digest = build_reminder_digest(tasks)
    assert 'tg://user?id=555">Кирилл</a>' in digest
    assert "«Купить домен»" in digest


def test_claimer_without_user_id_falls_back_to_bold_name():
    tasks = [_task(id=1, title="Задача", claimed_by="Кто-то", claimed_by_user_id=None)]
    digest = build_reminder_digest(tasks)
    assert "<b>Кто-то</b>" in digest
    assert "tg://user" not in digest


def test_unclaimed_tasks_get_their_own_section():
    tasks = [_task(id=1, title="Свободная задача", claimed_by=None)]
    digest = build_reminder_digest(tasks)
    assert "Пока никто не взял" in digest
    assert "«Свободная задача»" in digest


def test_testing_status_gets_suffix():
    tasks = [_task(id=1, title="В тесте", status="testing", claimed_by="Аня", claimed_by_user_id=1)]
    digest = build_reminder_digest(tasks)
    assert "«В тесте» (тестирование)" in digest


def test_html_is_escaped():
    tasks = [_task(id=1, title="<script>alert(1)</script>", claimed_by="A&B", claimed_by_user_id=1)]
    digest = build_reminder_digest(tasks)
    assert "<script>" not in digest
    assert "&lt;script&gt;" in digest
    assert "A&amp;B" in digest
