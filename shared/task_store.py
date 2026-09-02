from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Comment:
    author: str
    text: str
    created_at: str


@dataclass
class Photo:
    id: int
    file_name: str
    added_by: str
    created_at: str
    caption: str | None = None


@dataclass
class Task:
    id: int
    title: str
    status: str  # "open" | "testing" | "done" | "cancelled"
    claimed_by: str | None
    created_by: str
    created_at: str
    completed_at: str | None
    # uid зеркальной записи в iCloud Reminders (см. team_bot/main.py) — None,
    # если зеркалирование не настроено или упало (доска остаётся источником
    # правды в любом случае).
    reminder_uid: str | None = None
    # Telegram user id того, кто взял задачу — нужен для настоящего
    # @упоминания в ежедневном дайджесте (shared/reminder_digest.py), просто
    # имени недостаточно, чтобы Telegram подсветил и уведомил человека.
    claimed_by_user_id: int | None = None
    description: str | None = None
    comments: list[Comment] = field(default_factory=list)
    photos: list[Photo] = field(default_factory=list)
    # Заполняется только при status="cancelled" — для недельного
    # спринт-дайджеста (shared/sprint_digest.py), симметрично completed_at.
    cancelled_at: str | None = None

    # --- Планирование (TASK-SYS-1) ---
    # Эпик из закрытого списка shared/epics.py. Пусто = «не разобрано»:
    # это нормальный исход классификации, а не ошибка.
    epic: str | None = None
    # 0 — сначала, 3 — когда дойдут руки. Числом, а не словом: словами
    # «важно/очень важно/критично» команда за месяц перестаёт различать.
    priority: int = 2
    # Спринт, в который задача взята. None = в бэклоге.
    sprint_id: int | None = None
    # Грубая оценка в часах. Нужна не для отчётности, а чтобы предложение
    # распределения не сваливало на человека двадцать задач разом.
    estimate_hours: float | None = None

    # --- Синхронизация (TASK-SYS-1) ---
    # Когда задачу трогали в последний раз. Основа разрешения конфликтов:
    # без этого поля нельзя понять, чья правка свежее.
    updated_at: str | None = None
    # Откуда задача пришла: bot | miro | reminders | import | встреча.
    origin: str | None = None
    # Идентификатор карточки в Miro. None — карточки ещё нет.
    miro_item_id: str | None = None


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

    # Колонки, добавленные после первой версии схемы — для файлов БД,
    # созданных до них, накатываем через ALTER TABLE (см. _init_schema).
    # Новую версию схемы просто дополняй этим словарём, без ручных миграций.
    _ADDED_COLUMNS = {
        "claimed_by_user_id": "INTEGER",
        "description": "TEXT",
        "cancelled_at": "TEXT",
        # TASK-SYS-1: планирование и синхронизация с Miro/Напоминаниями.
        "epic": "TEXT",
        "priority": "INTEGER NOT NULL DEFAULT 2",
        "sprint_id": "INTEGER",
        "estimate_hours": "REAL",
        "updated_at": "TEXT",
        "origin": "TEXT",
        "miro_item_id": "TEXT",
    }

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
            existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            for column, sql_type in self._ADDED_COLUMNS.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {sql_type}")
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
            # Файлы самих фото лежат на диске (TaskBoardConfig.photos_dir) —
            # тут только метаданные, как у task_comments.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    caption TEXT,
                    added_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def add_task(
        self,
        title: str,
        created_by: str,
        description: str = "",
        *,
        epic: str | None = None,
        priority: int = 2,
        origin: str | None = None,
        estimate_hours: float | None = None,
    ) -> Task:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (title, status, created_by, created_at, description,"
                " epic, priority, origin, estimate_hours, updated_at)"
                " VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title, created_by, now, description or None,
                    epic, priority, origin, estimate_hours, now,
                ),
            )
            task_id = cursor.lastrowid
        return self.get_task(task_id)

    # --- Планирование (TASK-SYS-1) ---

    def set_epic(self, task_id: int, epic: str | None) -> Task:
        return self._update_or_raise(task_id, "UPDATE tasks SET epic = ? WHERE id = ?", (epic, task_id))

    def set_priority(self, task_id: int, priority: int) -> Task:
        # Диапазон режем здесь, а не у вызывающего: приоритет приходит и из
        # чата, и из Miro, и из импорта — проверять в трёх местах бессмысленно.
        priority = max(0, min(3, int(priority)))
        return self._update_or_raise(
            task_id, "UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id)
        )

    def set_sprint(self, task_id: int, sprint_id: int | None) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET sprint_id = ? WHERE id = ?", (sprint_id, task_id)
        )

    def set_estimate(self, task_id: int, hours: float | None) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET estimate_hours = ? WHERE id = ?", (hours, task_id)
        )

    def set_miro_item_id(self, task_id: int, item_id: str | None) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET miro_item_id = ? WHERE id = ?", (item_id, task_id)
        )

    def list_by_sprint(self, sprint_id: int) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE sprint_id = ? AND status != 'cancelled'"
                " ORDER BY priority, (status = 'done'), created_at",
                (sprint_id,),
            ).fetchall()
            return [self._hydrate(conn, row) for row in rows]

    def list_backlog(self) -> list[Task]:
        """Что не взято ни в один спринт и ещё не сделано — из этого набирают."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE sprint_id IS NULL AND status = 'open'"
                " ORDER BY priority, created_at"
            ).fetchall()
            return [self._hydrate(conn, row) for row in rows]

    def find_by_miro_item(self, item_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE miro_item_id = ?", (item_id,)).fetchone()
            return self._hydrate(conn, row) if row else None

    def find_by_reminder_uid(self, uid: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE reminder_uid = ?", (uid,)).fetchone()
            return self._hydrate(conn, row) if row else None

    def list_tasks(self, *, include_done: bool = True, done_within_days: int | None = None) -> list[Task]:
        """done_within_days: если задан (и include_done=True), задачи со
        статусом 'done', завершённые раньше этого срока, не возвращаются —
        доска не должна расти в бесконечную ленту старых готовых задач.
        Полная история всё равно доступна через include_done=True без этого
        параметра (мини-апп даёт переключатель "показать всю историю").
        Отменённые задачи ('cancelled') сюда никогда не попадают — это
        отдельная категория, видна только в недельном спринт-дайджесте
        (см. list_cancelled_since), не в обычном списке доски."""
        query = "SELECT * FROM tasks WHERE status != 'cancelled'"
        params: list[str] = []
        if not include_done:
            query += " AND status != 'done'"
        elif done_within_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=done_within_days)).isoformat()
            query += " AND (status != 'done' OR completed_at >= ?)"
            params.append(cutoff)
        query += " ORDER BY (status = 'done'), created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            tasks = [_row_to_task(row) for row in rows]
            for task in tasks:
                task.comments = self._load_comments(conn, task.id)
                task.photos = self._load_photos(conn, task.id)
        return tasks

    def list_done_since(self, since: datetime) -> list[Task]:
        """Для недельного спринт-дайджеста (shared/sprint_digest.py) —
        задачи, завершённые с указанного момента."""
        return self._list_by_status_since("done", "completed_at", since)

    def list_cancelled_since(self, since: datetime) -> list[Task]:
        """Для недельного спринт-дайджеста — задачи, отменённые с указанного
        момента (см. cancel_task)."""
        return self._list_by_status_since("cancelled", "cancelled_at", since)

    def _list_by_status_since(self, status: str, timestamp_column: str, since: datetime) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status = ? AND {timestamp_column} >= ? ORDER BY {timestamp_column}",
                (status, since.isoformat()),
            ).fetchall()
            tasks = [_row_to_task(row) for row in rows]
            for task in tasks:
                task.comments = self._load_comments(conn, task.id)
                task.photos = self._load_photos(conn, task.id)
        return tasks

    def get_task(self, task_id: int) -> Task:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
            task = _row_to_task(row)
            task.comments = self._load_comments(conn, task_id)
            task.photos = self._load_photos(conn, task_id)
        return task

    def claim_task(self, task_id: int, claimed_by: str, claimed_by_user_id: int | None = None) -> Task:
        return self._update_or_raise(
            task_id,
            "UPDATE tasks SET claimed_by = ?, claimed_by_user_id = ? WHERE id = ?",
            (claimed_by, claimed_by_user_id, task_id),
        )

    def unclaim_task(self, task_id: int) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET claimed_by = NULL, claimed_by_user_id = NULL WHERE id = ?", (task_id,)
        )

    def mark_testing(self, task_id: int) -> Task:
        return self._update_or_raise(task_id, "UPDATE tasks SET status = 'testing' WHERE id = ?", (task_id,))

    def complete_task(self, task_id: int) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (_now(), task_id)
        )

    def cancel_task(self, task_id: int) -> Task:
        # Отдельно от complete_task — "отменили" и "сделали" разные исходы
        # для недельного спринт-дайджеста (см. list_cancelled_since).
        return self._update_or_raise(
            task_id, "UPDATE tasks SET status = 'cancelled', cancelled_at = ? WHERE id = ?", (_now(), task_id)
        )

    def reopen_task(self, task_id: int) -> Task:
        # Возвращает в "open" из любого статуса (testing/done/cancelled) —
        # единая операция, а не отдельная для каждого предыдущего состояния.
        return self._update_or_raise(
            task_id,
            "UPDATE tasks SET status = 'open', completed_at = NULL, cancelled_at = NULL WHERE id = ?",
            (task_id,),
        )

    def set_reminder_uid(self, task_id: int, reminder_uid: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET reminder_uid = ? WHERE id = ?", (reminder_uid, task_id))

    def set_description(self, task_id: int, description: str) -> Task:
        return self._update_or_raise(
            task_id, "UPDATE tasks SET description = ? WHERE id = ?", (description or None, task_id)
        )

    def rename_task(self, task_id: int, title: str) -> Task:
        return self._update_or_raise(task_id, "UPDATE tasks SET title = ? WHERE id = ?", (title, task_id))

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

    def add_photo(self, task_id: int, file_name: str, added_by: str, caption: str = "") -> Task:
        """file_name — имя файла на диске (TaskBoardConfig.photos_dir), не оригинальное
        имя из Telegram: см. team_bot/main.py cmd_photo, там же качается сам файл."""
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
            conn.execute(
                "INSERT INTO task_photos (task_id, file_name, caption, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, file_name, caption or None, added_by, _now()),
            )
        return self.get_task(task_id)

    def _update_or_raise(self, task_id: int, sql: str, params: tuple) -> Task:
        with self._connect() as conn:
            result = conn.execute(sql, params)
            if result.rowcount == 0:
                raise TaskNotFound(f"Задача #{task_id} не найдена")
            # Отметку времени ставим ЗДЕСЬ, а не в каждом методе: через этот
            # helper проходят все изменения задачи, и так её невозможно
            # забыть. От неё зависит разрешение конфликтов при синхронизации
            # с Miro и Напоминаниями — пропущенная отметка означает потерянную
            # правку, причём молча.
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_now(), task_id))
        return self.get_task(task_id)

    def _hydrate(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
        """Строка БД в готовую задачу вместе с комментариями и фото.

        Вынесено отдельно, потому что выборок стало несколько (спринт, бэклог,
        поиск по внешнему id), и повторять три строки в каждой — верный способ
        однажды забыть комментарии в одной из них.
        """
        task = _row_to_task(row)
        task.comments = self._load_comments(conn, task.id)
        task.photos = self._load_photos(conn, task.id)
        return task

    def _load_comments(self, conn: sqlite3.Connection, task_id: int) -> list[Comment]:
        rows = conn.execute(
            "SELECT author, text, created_at FROM task_comments WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [Comment(author=row["author"], text=row["text"], created_at=row["created_at"]) for row in rows]

    def _load_photos(self, conn: sqlite3.Connection, task_id: int) -> list[Photo]:
        rows = conn.execute(
            "SELECT id, file_name, caption, added_by, created_at FROM task_photos"
            " WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [
            Photo(
                id=row["id"],
                file_name=row["file_name"],
                caption=row["caption"],
                added_by=row["added_by"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


def _column(row: sqlite3.Row, name: str, default=None):
    """Значение колонки, которой может не быть в старом файле БД.

    ALTER TABLE в _init_schema их добавляет, но чтение может произойти и до
    первого запуска новой версии — например, из другого процесса (webapp),
    который поднялся раньше. Падать на этом нельзя: доска важнее полей.
    """
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


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
        claimed_by_user_id=row["claimed_by_user_id"],
        description=row["description"],
        cancelled_at=row["cancelled_at"],
        epic=_column(row, "epic"),
        priority=int(_column(row, "priority", 2)),
        sprint_id=_column(row, "sprint_id"),
        estimate_hours=_column(row, "estimate_hours"),
        updated_at=_column(row, "updated_at"),
        origin=_column(row, "origin"),
        miro_item_id=_column(row, "miro_item_id"),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
