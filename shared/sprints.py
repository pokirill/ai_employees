"""Спринты и заявленная ёмкость людей (TASK-SYS-1).

## Почему спринт стал сущностью

Раньше «спринт» был скользящим окном: последняя отметка времени в JSON-файле и
«всё, что закрыли с тех пор». Для короткой сводки этого хватало, но для доски —
нет. Нельзя ответить на вопросы, ради которых спринт и заводят: что мы взяли на
эти две недели, кто сколько взял, что не влезло и почему, и стало ли лучше по
сравнению с прошлым разом.

Поэтому спринт теперь запись в базе: даты, цель, состав задач и заявленная
ёмкость каждого. Старый механизм скользящего окна не трогаем — он обслуживает
ежедневные напоминания и продолжает работать сам по себе.

## Две недели

Не настройка, а решение. Неделя не оставляет времени на работу, которая длиннее
пары дней, и превращает планирование в еженедельный ритуал ради ритуала. Месяц
слишком долго: к середине план уже не про то. Две недели меняются вместе с
задачами, поэтому длительность фиксированная, а не «сколько получится».

## Ёмкость заявляет человек, а не система

Никаких «нагрузим по 40 часов». Человек в начале спринта говорит своими
словами: занят, нормально, свободен — или называет часы, если хочет точнее.
Это заявление, а не измерение: система не считает чужие часы и не спорит с
человеком о его занятости.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SPRINT_DAYS = 14

# Уровни занятости. Человеку проще выбрать слово, чем назвать часы, а нам для
# распределения достаточно порядка величины. Часы можно указать отдельно.
LEVEL_BUSY = "busy"
LEVEL_NORMAL = "normal"
LEVEL_FREE = "free"

LEVEL_TITLES = {
    LEVEL_BUSY: "занят",
    LEVEL_NORMAL: "обычная загрузка",
    LEVEL_FREE: "есть время",
}

# Ориентир в часах на спринт, если человек назвал только слово. Числа
# намеренно скромные: план, который не выполняется, хуже отсутствия плана.
LEVEL_DEFAULT_HOURS = {
    LEVEL_BUSY: 8.0,
    LEVEL_NORMAL: 20.0,
    LEVEL_FREE: 32.0,
}

STATUS_PLANNED = "planned"
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"


@dataclass
class Capacity:
    person: str
    person_user_id: int | None
    level: str
    hours: float
    note: str | None
    declared_at: str

    @property
    def level_title(self) -> str:
        return LEVEL_TITLES.get(self.level, self.level)


@dataclass
class Sprint:
    id: int
    title: str
    goal: str | None
    starts_at: str
    ends_at: str
    status: str
    created_at: str
    # Доска этого спринта. Пусто — берётся общая из настроек.
    miro_board_id: str | None = None
    capacities: list[Capacity] = field(default_factory=list)

    @property
    def period_label(self) -> str:
        return f"{_short(self.starts_at)} — {_short(self.ends_at)}"

    def days_left(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        end = datetime.fromisoformat(self.ends_at)
        return max(0, (end - now).days)


class SprintNotFound(RuntimeError):
    pass


class SprintStore:
    """Спринты в том же файле SQLite, что и задачи.

    Отдельный файл был бы ошибкой: тогда «задачи спринта» пришлось бы собирать
    join-ом через границу двух баз, а это либо ручная склейка в Python, либо
    ATTACH — и то и другое хуже, чем одна база.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    goal TEXT,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    miro_board_id TEXT
                )
                """
            )
            # Для баз, созданных до появления колонки.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(sprints)").fetchall()}
            if "miro_board_id" not in existing:
                conn.execute("ALTER TABLE sprints ADD COLUMN miro_board_id TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sprint_capacity (
                    sprint_id INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
                    person TEXT NOT NULL,
                    person_user_id INTEGER,
                    level TEXT NOT NULL,
                    hours REAL NOT NULL,
                    note TEXT,
                    declared_at TEXT NOT NULL,
                    PRIMARY KEY (sprint_id, person)
                )
                """
            )

    # ------------------------------------------------------------------
    # Спринты
    # ------------------------------------------------------------------

    def current(self) -> Sprint | None:
        """Активный спринт или None. Не создаём его молча: спринт начинается
        решением команды, а не тем, что кто-то открыл доску."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sprints WHERE status = ? ORDER BY starts_at DESC LIMIT 1",
                (STATUS_ACTIVE,),
            ).fetchone()
            return self._hydrate(conn, row) if row else None

    def get(self, sprint_id: int) -> Sprint:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
            if row is None:
                raise SprintNotFound(f"Спринт #{sprint_id} не найден")
            return self._hydrate(conn, row)

    def start(self, *, title: str = "", goal: str = "", now: datetime | None = None) -> Sprint:
        """Открыть спринт. Предыдущий активный закрывается автоматически:
        два активных спринта одновременно — это не два плана, а отсутствие
        плана."""
        now = now or datetime.now(timezone.utc)
        previous = self.current()
        if previous is not None:
            self.close(previous.id)
        ends = now + timedelta(days=SPRINT_DAYS)
        title = title.strip() or f"Спринт {_short(now.isoformat())} — {_short(ends.isoformat())}"
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sprints (title, goal, starts_at, ends_at, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (title, goal.strip() or None, now.isoformat(), ends.isoformat(),
                 STATUS_ACTIVE, now.isoformat()),
            )
            sprint_id = cursor.lastrowid
        return self.get(sprint_id)

    def close(self, sprint_id: int) -> Sprint:
        with self._connect() as conn:
            conn.execute("UPDATE sprints SET status = ? WHERE id = ?", (STATUS_CLOSED, sprint_id))
        return self.get(sprint_id)

    def set_board(self, sprint_id: int, board_id: str | None) -> Sprint:
        with self._connect() as conn:
            conn.execute("UPDATE sprints SET miro_board_id = ? WHERE id = ?", (board_id, sprint_id))
        return self.get(sprint_id)

    def set_goal(self, sprint_id: int, goal: str) -> Sprint:
        with self._connect() as conn:
            conn.execute("UPDATE sprints SET goal = ? WHERE id = ?", (goal.strip() or None, sprint_id))
        return self.get(sprint_id)

    def recent(self, limit: int = 5) -> list[Sprint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sprints ORDER BY starts_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._hydrate(conn, row) for row in rows]

    # ------------------------------------------------------------------
    # Ёмкость
    # ------------------------------------------------------------------

    def declare_capacity(
        self,
        sprint_id: int,
        *,
        person: str,
        person_user_id: int | None,
        level: str,
        hours: float | None = None,
        note: str = "",
    ) -> Capacity:
        """Записать заявление человека о своей загрузке.

        Перезаписываем, а не копим историю: важно последнее слово человека.
        «Я передумал, стало свободнее» должно просто работать.
        """
        level = level if level in LEVEL_DEFAULT_HOURS else LEVEL_NORMAL
        resolved_hours = float(hours) if hours else LEVEL_DEFAULT_HOURS[level]
        declared_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sprint_capacity (sprint_id, person, person_user_id, level, hours, note, declared_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(sprint_id, person) DO UPDATE SET"
                " person_user_id = excluded.person_user_id, level = excluded.level,"
                " hours = excluded.hours, note = excluded.note, declared_at = excluded.declared_at",
                (sprint_id, person, person_user_id, level, resolved_hours, note.strip() or None, declared_at),
            )
        return Capacity(
            person=person,
            person_user_id=person_user_id,
            level=level,
            hours=resolved_hours,
            note=note.strip() or None,
            declared_at=declared_at,
        )

    def capacities(self, sprint_id: int) -> list[Capacity]:
        with self._connect() as conn:
            return self._capacities(conn, sprint_id)

    # ------------------------------------------------------------------

    def _hydrate(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Sprint:
        sprint = Sprint(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            status=row["status"],
            created_at=row["created_at"],
            miro_board_id=row["miro_board_id"] if "miro_board_id" in row.keys() else None,
        )
        sprint.capacities = self._capacities(conn, sprint.id)
        return sprint

    def _capacities(self, conn: sqlite3.Connection, sprint_id: int) -> list[Capacity]:
        rows = conn.execute(
            "SELECT * FROM sprint_capacity WHERE sprint_id = ? ORDER BY person", (sprint_id,)
        ).fetchall()
        return [
            Capacity(
                person=row["person"],
                person_user_id=row["person_user_id"],
                level=row["level"],
                hours=row["hours"],
                note=row["note"],
                declared_at=row["declared_at"],
            )
            for row in rows
        ]


def _short(iso: str) -> str:
    """«2026-09-02T...» → «2 сен». Для заголовков и сводок."""
    months = ("янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    return f"{dt.day} {months[dt.month - 1]}"
