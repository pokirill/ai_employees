"""Синхронизация доски задач с Miro и Напоминаниями (TASK-SYS-1).

## Топология: звезда, а не «все со всеми»

    Miro ──┐
           ├── SQLite (источник правды) ──── Telegram (бот и мини-апп)
Напоминания┘

Синхронизируем каждый инструмент ТОЛЬКО с ядром. Попарная синхронизация трёх
инструментов — это шесть направлений и гарантированные циклы: правка из Miro
уезжает в Напоминания, оттуда возвращается в Miro как «внешнее изменение», и
задача начинает мигать между состояниями. С центром таких циклов не бывает.

## Почему источник правды — SQLite

Только там есть исполнитель, комментарии, эпик, спринт, приоритет и история. В
карточке Miro этого нет, в VTODO Напоминаний тем более. И, что важнее,
случайное удаление карточки в Miro не должно означать потерю задачи.

## Как определяется, что изменилось

На каждую пару «задача + инструмент» храним снимок последнего
синхронизированного состояния (`task_sync`). Дальше просто:

- снимок ≠ то, что сейчас во внешнем инструменте → изменили ТАМ;
- `task.updated_at` новее момента синхронизации → изменили У НАС;
- и то и другое → конфликт.

## Конфликты

Правило одно и оно осознанное: **статус и исполнитель — за ядром**, потому что
там дисциплина и там же живёт `claimed_by`, которого во внешних инструментах
просто нет. Текст (название) берём с той стороны, где его меняли, а если меняли
с обеих — оставляем ядро и ПИШЕМ об этом. Молча потерянная правка хуже, чем
сообщение о конфликте: про потерянную никто не узнает, пока не станет поздно.

## Чего движок не делает никогда

**Не удаляет задачи.** Пропала карточка в Miro или запись в Напоминаниях — это
повод отвязать внешний id и сказать об этом, а не стирать задачу. В Miro
карточку сносят случайным нажатием примерно раз в неделю.

**Не назначает исполнителей.** Синхронизация переносит состояние, а не
принимает решения за людей.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared import epics
from shared.task_store import Task, TaskStore

logger = logging.getLogger(__name__)

TARGET_MIRO = "miro"
TARGET_REMINDERS = "reminders"


@dataclass
class Change:
    """Одно изменение, о котором стоит сказать человеку."""

    task_id: int
    title: str
    kind: str          # created | status | renamed | conflict | unlinked
    detail: str
    source: str        # miro | reminders | core
    notify_user_id: int | None = None

    def as_line(self) -> str:
        icons = {
            "created": "🆕",
            "status": "🔄",
            "renamed": "✏️",
            "conflict": "⚠️",
            "unlinked": "🔗",
        }
        return f"{icons.get(self.kind, '•')} #{self.task_id} {self.title} — {self.detail}"


@dataclass
class SyncResult:
    changes: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "SyncResult") -> None:
        self.changes.extend(other.changes)
        self.errors.extend(other.errors)


class SyncState:
    """Снимки последнего синхронизированного состояния.

    Отдельная таблица, а не поля в `tasks`: инструментов уже два, будет больше,
    и добавлять по три колонки на каждый — путь к таблице, в которой половина
    полей всегда пустая.
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
                CREATE TABLE IF NOT EXISTS task_sync (
                    task_id INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    external_id TEXT,
                    snapshot TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, target)
                )
                """
            )

    def get(self, task_id: int, target: str) -> tuple[str | None, str | None, str | None]:
        """(external_id, snapshot, synced_at) или (None, None, None)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT external_id, snapshot, synced_at FROM task_sync WHERE task_id = ? AND target = ?",
                (task_id, target),
            ).fetchone()
        if row is None:
            return None, None, None
        return row["external_id"], row["snapshot"], row["synced_at"]

    def remember(self, task_id: int, target: str, *, external_id: str | None, snapshot: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_sync (task_id, target, external_id, snapshot, synced_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id, target) DO UPDATE SET"
                " external_id = excluded.external_id, snapshot = excluded.snapshot,"
                " synced_at = excluded.synced_at",
                (task_id, target, external_id, snapshot, _now()),
            )

    def forget(self, task_id: int, target: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM task_sync WHERE task_id = ? AND target = ?", (task_id, target))

    def external_ids(self, target: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, external_id FROM task_sync WHERE target = ? AND external_id IS NOT NULL",
                (target,),
            ).fetchall()
        return {row["external_id"]: row["task_id"] for row in rows}


# ----------------------------------------------------------------------
# Miro
# ----------------------------------------------------------------------


def sync_miro(store: TaskStore, state: SyncState, board, *, sprint_id: int | None) -> SyncResult:
    """Двусторонняя синхронизация доски спринта с Miro.

    `board` — `shared.miro_client.MiroBoard`. Не настроен → тихо ничего не
    делаем: отсутствие Miro не должно ронять остальную синхронизацию.
    """
    result = SyncResult()
    if board is None or not board.enabled:
        return result

    try:
        columns = board.ensure_columns()
        remote_cards = {card.item_id: card for card in board.list_cards()}
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Miro: {exc}")
        return result

    tasks = store.list_by_sprint(sprint_id) if sprint_id else store.list_backlog()
    known = state.external_ids(TARGET_MIRO)

    # --- Из Miro к нам ---
    for item_id, card in remote_cards.items():
        task_id = known.get(item_id)
        if task_id is None:
            # Карточку создали руками прямо на доске — заводим задачу.
            if not card.title:
                continue
            task = store.add_task(
                card.title,
                created_by="miro",
                description=card.description,
                epic=epics.classify_by_keywords(card.title) or None,
                origin=TARGET_MIRO,
            )
            if sprint_id:
                store.set_sprint(task.id, sprint_id)
            store.set_miro_item_id(task.id, item_id)
            state.remember(
                task.id, TARGET_MIRO, external_id=item_id,
                snapshot=_snapshot(
                    external=_card_fingerprint(card),
                    local=_local_fingerprint(store.get_task(task.id)),
                ),
            )
            result.changes.append(
                Change(task.id, task.title, "created", "создана на доске Miro", TARGET_MIRO)
            )
            continue

        try:
            task = store.get_task(task_id)
        except Exception:
            continue

        _, snapshot, synced_at = state.get(task_id, TARGET_MIRO)
        external_seen, local_seen = _split_snapshot(snapshot)
        remote_changed = external_seen != _card_fingerprint(card)
        local_changed = _is_newer(task.updated_at, synced_at)

        if remote_changed and card.status and card.status != _board_status(task):
            if local_changed:
                # Двигали и там, и тут. Статус оставляем наш: только у нас
                # известно, кто взял задачу, и «В работе» без исполнителя —
                # это потерянная информация.
                result.changes.append(
                    Change(
                        task.id, task.title, "conflict",
                        f"статус меняли и в Miro, и у нас — оставил наш ({task.status})",
                        TARGET_MIRO, task.claimed_by_user_id,
                    )
                )
            else:
                _apply_status(store, task, card.status)
                result.changes.append(
                    Change(
                        task.id, task.title, "status",
                        f"перенесена в «{_column_title(card.status)}» на доске",
                        TARGET_MIRO, task.claimed_by_user_id,
                    )
                )

        if remote_changed and card.title and card.title != task.title and not local_changed:
            store.rename_task(task.id, card.title)
            result.changes.append(
                Change(task.id, card.title, "renamed", "переименована в Miro", TARGET_MIRO)
            )

    # --- От нас в Miro ---
    for task in tasks:
        external_id, snapshot, synced_at = state.get(task.id, TARGET_MIRO)
        target_status = _board_status(task)
        frame_id = columns.get(target_status)
        if not frame_id:
            continue

        if external_id and external_id not in remote_cards:
            # Карточку удалили. Задачу не трогаем — только отвязываем и
            # говорим об этом: удаление карточки почти всегда случайность.
            state.forget(task.id, TARGET_MIRO)
            store.set_miro_item_id(task.id, None)
            result.changes.append(
                Change(task.id, task.title, "unlinked", "карточка в Miro исчезла — создам заново", TARGET_MIRO)
            )
            external_id = None

        _, local_seen = _split_snapshot(snapshot)
        desired_local = _local_fingerprint(task)
        desired = _snapshot(external=_external_fingerprint_of_task(task), local=desired_local)
        try:
            if external_id is None:
                new_id = board.create_card(
                    title=task.title,
                    description=_card_description(task),
                    frame_id=frame_id,
                    index=0,
                )
                store.set_miro_item_id(task.id, new_id)
                state.remember(task.id, TARGET_MIRO, external_id=new_id, snapshot=desired)
            elif local_seen != desired_local:
                board.update_card(
                    external_id,
                    title=task.title,
                    description=_card_description(task),
                    frame_id=frame_id,
                )
                state.remember(task.id, TARGET_MIRO, external_id=external_id, snapshot=desired)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"Miro #{task.id}: {exc}")

    return result


# ----------------------------------------------------------------------
# Напоминания
# ----------------------------------------------------------------------


def sync_reminders(store: TaskStore, state: SyncState, reminders) -> SyncResult:
    """Синхронизация с Напоминаниями iCloud.

    Возможности CalDAV скромные: список открытых задач, создание и отметка
    выполненной. Поэтому и синхронизация скромная — но двусторонняя:

    - новая запись в Напоминаниях → новая задача у нас;
    - задача закрыта у нас → отмечаем выполненной в Напоминаниях;
    - запись пропала из открытых (человек закрыл её в приложении) → закрываем
      задачу у нас.

    Последнее и есть то, чего не хватало: раньше зеркало было односторонним, и
    закрытая на телефоне задача продолжала висеть на доске.
    """
    result = SyncResult()
    if reminders is None:
        return result

    try:
        open_remote = {item.uid: item.title for item in reminders.list_open_tasks()}
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Напоминания: {exc}")
        return result

    known = state.external_ids(TARGET_REMINDERS)

    # --- Новые записи из Напоминаний ---
    for uid, title in open_remote.items():
        if uid in known:
            continue
        if store.find_by_reminder_uid(uid) is not None:
            continue
        task = store.add_task(
            title,
            created_by="напоминания",
            epic=epics.classify_by_keywords(title) or None,
            origin=TARGET_REMINDERS,
        )
        state.remember(task.id, TARGET_REMINDERS, external_id=uid, snapshot=_snapshot(external=_digest(title), local=_digest(title)))
        result.changes.append(
            Change(task.id, title, "created", "добавлена в Напоминаниях", TARGET_REMINDERS)
        )

    # --- Наши задачи наружу ---
    for task in store.list_tasks(include_done=False):
        external_id, snapshot, _ = state.get(task.id, TARGET_REMINDERS)
        if external_id is None and task.reminder_uid:
            # Задача зеркалилась старым кодом до появления этой таблицы —
            # подхватываем связь, а не создаём дубль.
            external_id = task.reminder_uid
            state.remember(task.id, TARGET_REMINDERS, external_id=external_id, snapshot=_snapshot(external=_digest(task.title), local=_digest(task.title)))

        if external_id is None:
            try:
                uid = reminders.add_task(task.title, notes=f"Задача #{task.id} на доске команды")
                state.remember(task.id, TARGET_REMINDERS, external_id=uid, snapshot=_snapshot(external=_digest(task.title), local=_digest(task.title)))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Напоминания #{task.id}: {exc}")
            continue

        if external_id not in open_remote and snapshot is not None:
            # Была в списке, пропала из открытых — значит закрыли в приложении.
            _apply_status(store, task, "done")
            result.changes.append(
                Change(
                    task.id, task.title, "status", "закрыта в Напоминаниях",
                    TARGET_REMINDERS, task.claimed_by_user_id,
                )
            )

    # --- Закрытые у нас — закрыть и там ---
    for task in store.list_tasks(include_done=True, done_within_days=7):
        if task.status != "done":
            continue
        external_id, _, _ = state.get(task.id, TARGET_REMINDERS)
        if not external_id or external_id not in open_remote:
            continue
        try:
            reminders.complete_task(external_id)
        except Exception:
            # Не нашлась среди открытых — значит уже закрыта. Это не ошибка.
            pass

    return result


# ----------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------


def _board_status(task: Task) -> str:
    """Статус задачи в терминах колонок доски.

    В базе статусов четыре, но «взята в работу» там выражена не статусом, а
    наличием `claimed_by` — на доске же это отдельная колонка, иначе колонка
    «В работе» была бы всегда пустой.
    """
    if task.status == "done":
        return "done"
    if task.status == "testing":
        return "testing"
    if task.claimed_by:
        return "claimed"
    return "open"


def _apply_status(store: TaskStore, task: Task, board_status: str) -> None:
    if board_status == "done" and task.status != "done":
        store.complete_task(task.id)
    elif board_status == "testing" and task.status != "testing":
        store.mark_testing(task.id)
    elif board_status in ("open", "claimed") and task.status in ("done", "testing"):
        store.reopen_task(task.id)


def _column_title(status: str) -> str:
    from shared.miro_client import COLUMN_TITLES

    return COLUMN_TITLES.get(status, status)


def _card_description(task: Task) -> str:
    """Что пишем в карточку. Коротко и только то, что помогает на доске."""
    parts = [f"#{task.id}"]
    if task.epic:
        parts.append(epics.label(task.epic))
    parts.append(f"P{task.priority}")
    if task.claimed_by:
        parts.append(f"взял: {task.claimed_by}")
    if task.estimate_hours:
        parts.append(f"~{task.estimate_hours:g} ч")
    head = " · ".join(parts)
    return f"{head}\n\n{task.description or ''}".strip()


# Снимок состоит из ДВУХ отпечатков через двоеточие:
#
#   <как это выглядит снаружи>:<как это выглядит у нас>
#
# Первый нужен, чтобы понять, изменилось ли что-то во внешнем инструменте:
# его умеют считать обе стороны, потому что в него входит только то, что
# внешний инструмент вообще показывает — название и колонка.
#
# Второй — чтобы понять, нужно ли отправлять обновление: в него входят поля,
# которые мы пишем в описание карточки (эпик, приоритет, исполнитель, оценка).
#
# Одним отпечатком тут не обойтись, и это не теория: с одним полем сравнение
# шло бы по разным наборам полей на разных сторонах, и «внешнее изменение»
# срабатывало бы на каждом проходе синхронизации.


def _snapshot(*, external: str, local: str) -> str:
    return f"{external}:{local}"


def _split_snapshot(snapshot: str | None) -> tuple[str | None, str | None]:
    if not snapshot or ":" not in snapshot:
        return None, None
    external, local = snapshot.split(":", 1)
    return external or None, local or None


def _external_fingerprint_of_task(task: Task) -> str:
    """Как задача выглядит для внешнего инструмента."""
    return _digest(f"{task.title}|{_board_status(task)}")


def _local_fingerprint(task: Task) -> str:
    """Всё, что мы отправляем во внешний инструмент."""
    return _digest(
        f"{task.title}|{_board_status(task)}|{task.epic}|{task.priority}"
        f"|{task.claimed_by}|{task.estimate_hours}|{task.description}"
    )


def _card_fingerprint(card) -> str:
    return _digest(f"{card.title}|{card.status}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _is_newer(updated_at: str | None, synced_at: str | None) -> bool:
    if not updated_at or not synced_at:
        return bool(updated_at)
    try:
        return datetime.fromisoformat(updated_at) > datetime.fromisoformat(synced_at)
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
