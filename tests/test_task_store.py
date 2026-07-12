from __future__ import annotations

import sqlite3

import pytest

from shared.task_store import TaskNotFound, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "tasks.db"))


def test_add_and_list_task(store):
    task = store.add_task("Купить домен", created_by="Аня")
    assert task.id > 0
    assert task.status == "open"
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Купить домен"


def test_list_tasks_excludes_done_when_requested(store):
    open_task = store.add_task("Открытая", created_by="Аня")
    done_task = store.add_task("Закрытая", created_by="Аня")
    store.complete_task(done_task.id)
    open_only = store.list_tasks(include_done=False)
    assert [t.id for t in open_only] == [open_task.id]


def test_claim_and_unclaim(store):
    task = store.add_task("Задача", created_by="Аня")
    claimed = store.claim_task(task.id, "Боря", claimed_by_user_id=42)
    assert claimed.claimed_by == "Боря"
    assert claimed.claimed_by_user_id == 42
    unclaimed = store.unclaim_task(task.id)
    assert unclaimed.claimed_by is None
    assert unclaimed.claimed_by_user_id is None


def test_mark_testing_and_reopen(store):
    task = store.add_task("Задача", created_by="Аня")
    testing = store.mark_testing(task.id)
    assert testing.status == "testing"
    reopened = store.reopen_task(task.id)
    assert reopened.status == "open"


def test_set_description(store):
    task = store.add_task("Задача", created_by="Аня")
    assert task.description is None
    updated = store.set_description(task.id, "Подробности задачи")
    assert updated.description == "Подробности задачи"


def test_add_task_with_description(store):
    task = store.add_task("Задача", created_by="Аня", description="Сразу с описанием")
    assert task.description == "Сразу с описанием"


def test_complete_and_reopen(store):
    task = store.add_task("Задача", created_by="Аня")
    done = store.complete_task(task.id)
    assert done.status == "done"
    assert done.completed_at is not None
    reopened = store.reopen_task(task.id)
    assert reopened.status == "open"
    assert reopened.completed_at is None


def test_add_comment_appends_in_order(store):
    task = store.add_task("Задача", created_by="Аня")
    store.add_comment(task.id, "Аня", "Первый")
    updated = store.add_comment(task.id, "Боря", "Второй")
    assert [c.text for c in updated.comments] == ["Первый", "Второй"]


def test_missing_task_raises(store):
    with pytest.raises(TaskNotFound):
        store.get_task(999)
    with pytest.raises(TaskNotFound):
        store.claim_task(999, "Аня")
    with pytest.raises(TaskNotFound):
        store.complete_task(999)
    with pytest.raises(TaskNotFound):
        store.add_comment(999, "Аня", "текст")
    with pytest.raises(TaskNotFound):
        store.mark_testing(999)
    with pytest.raises(TaskNotFound):
        store.set_description(999, "текст")


def test_set_reminder_uid(store):
    task = store.add_task("Задача", created_by="Аня")
    store.set_reminder_uid(task.id, "uid-123")
    assert store.get_task(task.id).reminder_uid == "uid-123"


def test_migrates_pre_existing_db_missing_new_columns(tmp_path):
    # Файл БД созданный до появления description/claimed_by_user_id — как
    # если бы кто-то запускал бота на старой версии кода.
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            claimed_by TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            reminder_uid TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (title, created_by, created_at) VALUES ('Старая задача', 'Аня', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = TaskStore(db_path)
    tasks = store.list_tasks()
    assert tasks[0].title == "Старая задача"
    assert tasks[0].description is None
    assert tasks[0].claimed_by_user_id is None
    # Новые колонки реально пишутся, не только читаются с дефолтом.
    updated = store.set_description(tasks[0].id, "Догнали описание")
    assert updated.description == "Догнали описание"
