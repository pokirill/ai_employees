from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Comment:
    author: str
    text: str
    created_at: str


@dataclass
class Task:
    id: int
    title: str
    status: str  # "open" | "done"
    claimed_by: str | None
    created_by: str
    created_at: str
    completed_at: str | None
    # uid зеркальной записи в iCloud Reminders (см. team_bot/main.py) — None,
    # если зеркалирование не настроено или упало (доска остаётся источником
    # правды в любом случае).
    reminder_uid: str | None = None
    comments: list[Comment] = field(default_factory=list)


class TaskNotFound(RuntimeError):
    pass


class TaskStore:
    """Общая доска задач команды — источник правды для мини-аппа. SQLite,
    не iCloud Reminders: у VTODO нет полей claimed_by/комментариев, а
    несколько человек должны видеть и менять одну и ту же задачу одновременно.
    team_bot зеркалирует новые задачи в Напоминания best-effort (см.
    team_bot/main.py) для тех, кто предпочитает привычный список на телефоне,
    но статус claim/done/комментарии живут только здесь."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
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
                """
                CREATE TABLE IF NOT EXISTS task_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def add_task(self, title: str, created_by: str) -> Task:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (title, status, created_by, created_at) VALUES (?, 'open', ?, ?)",
                (title, created_by, _now()),
            )
            task_id = cursor.lastrowid
        return self.get_task(task_id)

    def list_tasks(self, *, include_done: bool = True) -> list[Task]:
        query = "SELECT * FROM tasks"
        if not include_done:
            query += " WHERE status != 'done'"
        query += " ORDER BY (status = 'done'), created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
            tasks = [_row_to_task(row) for row in rows]
            for task in tasks:
                task.comments = self._load_comments(conn, task.id)
        return tasks

    def get_task(self, task_id: int) -> Task:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
            task = _row_to_task(row)
            task.comments = self._load_comments(conn, task_id)
        return task

    def claim_task(self, task_id: int, claimed_by: str) -> Task:
        return self._update_or_raise(task_id, "UPDATE tasks SET claimed_by = ? WHERE id = ?", (claimed_by, task_id))

    def unclaim_task(self, task_id: int) -> Task:
        return self._update_or_raise(task_id, "UPDATE tasks SET claimed_by = NULL WHERE id = ?", (task_id,))

    def complete_task(self, task_id: int) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (_now(), task_id)
        )

    def reopen_task(self, task_id: int) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET status = 'open', completed_at = NULL WHERE id = ?", (task_id,)
        )

    def set_reminder_uid(self, task_id: int, reminder_uid: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET reminder_uid = ? WHERE id = ?", (reminder_uid, task_id))

    def add_comment(self, task_id: int, author: str, text: str) -> Task:
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
            conn.execute(
                "INSERT INTO task_comments (task_id, author, text, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author, text, _now()),
            )
        return self.get_task(task_id)

    def _update_or_raise(self, task_id: int, sql: str, params: tuple) -> Task:
        with self._connect() as conn:
            result = conn.execute(sql, params)
            if result.rowcount == 0:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
        return self.get_task(task_id)

    def _load_comments(self, conn: sqlite3.Connection, task_id: int) -> list[Comment]:
        rows = conn.execute(
            "SELECT author, text, created_at FROM task_comments WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [Comment(author=row["author"], text=row["text"], created_at=row["created_at"]) for row in rows]


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        claimed_by=row["claimed_by"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        reminder_uid=row["reminder_uid"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
