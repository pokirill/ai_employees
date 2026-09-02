"""Движок синхронизации доски с Miro (TASK-SYS-1).

Проверяем на поддельной доске в памяти, а не на живом Miro: тест не должен
требовать токена и сети, иначе его никто не запустит.

Главное, что здесь проверяется, — идемпотентность. Синхронизация запускается по
кругу, и если второй проход что-то делает, задачи начинают размножаться
дублями. Это ровно тот дефект, который на живой доске заметят через сутки, когда
на ней будет двести карточек.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.miro_client import MiroCard  # noqa: E402
from shared.sync_engine import SyncState, sync_miro  # noqa: E402
from shared.task_store import TaskStore  # noqa: E402


class FakeMiro:
    """Доска в памяти с теми же операциями, что у настоящей."""

    enabled = True

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.frames = {status: f"frame-{status}" for status in ("open", "claimed", "testing", "done")}
        self._next = 1
        self.calls = {"create": 0, "update": 0}

    def ensure_columns(self) -> dict[str, str]:
        return dict(self.frames)

    def list_cards(self) -> list[MiroCard]:
        by_frame = {frame_id: status for status, frame_id in self.frames.items()}
        return [
            MiroCard(
                item_id=item_id,
                title=card["title"],
                description=card["desc"],
                frame_id=card["frame"],
                status=by_frame.get(card["frame"]),
            )
            for item_id, card in self.cards.items()
        ]

    def create_card(self, *, title: str, description: str, frame_id: str, index: int) -> str:
        item_id = f"card-{self._next}"
        self._next += 1
        self.cards[item_id] = {"title": title, "desc": description, "frame": frame_id}
        self.calls["create"] += 1
        return item_id

    def update_card(self, item_id: str, *, title: str, description: str, frame_id: str) -> None:
        self.cards[item_id] = {"title": title, "desc": description, "frame": frame_id}
        self.calls["update"] += 1

    # --- то, что делает человек мышкой ---

    def move(self, item_id: str, status: str) -> None:
        self.cards[item_id]["frame"] = self.frames[status]

    def rename(self, item_id: str, title: str) -> None:
        self.cards[item_id]["title"] = title

    def card_id(self, title: str) -> str:
        return next(item_id for item_id, card in self.cards.items() if card["title"] == title)

    def has(self, title: str) -> bool:
        return any(card["title"] == title for card in self.cards.values())


def _setup():
    db = tempfile.mktemp(suffix=".db")
    return db, TaskStore(db), SyncState(db), FakeMiro()


def test_our_tasks_go_to_the_board():
    db, store, state, miro = _setup()
    try:
        store.add_task("Починить дубли", created_by="тест")
        store.add_task("Написать пост", created_by="тест")
        result = sync_miro(store, state, miro, sprint_id=None)
        assert not result.errors, result.errors
        assert len(miro.cards) == 2
        assert miro.calls["create"] == 2
    finally:
        os.remove(db)


def test_second_pass_does_nothing():
    """Идемпотентность: повторный проход не создаёт ни карточек, ни задач."""
    db, store, state, miro = _setup()
    try:
        store.add_task("Починить дубли", created_by="тест")
        sync_miro(store, state, miro, sprint_id=None)
        before = dict(miro.calls)
        sync_miro(store, state, miro, sprint_id=None)
        assert miro.calls == before, f"{miro.calls} != {before}"
        assert len(store.list_backlog()) == 1
    finally:
        os.remove(db)


def test_card_moved_in_miro_changes_status():
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Починить дубли", created_by="тест")
        sync_miro(store, state, miro, sprint_id=None)
        miro.move(miro.card_id("Починить дубли"), "done")
        result = sync_miro(store, state, miro, sprint_id=None)
        assert store.get_task(task.id).status == "done"
        assert any(change.kind == "status" for change in result.changes)
    finally:
        os.remove(db)


def test_card_created_on_board_becomes_task():
    db, store, state, miro = _setup()
    try:
        miro.create_card(title="Задача с доски", description="", frame_id=miro.frames["open"], index=0)
        result = sync_miro(store, state, miro, sprint_id=None)
        assert any(task.title == "Задача с доски" for task in store.list_tasks())
        assert any(change.kind == "created" for change in result.changes)
        # И повтор не заводит дубль.
        sync_miro(store, state, miro, sprint_id=None)
        assert len([t for t in store.list_tasks() if t.title == "Задача с доски"]) == 1
    finally:
        os.remove(db)


def test_deleted_card_does_not_delete_task():
    """Карточку в Miro сносят случайным нажатием. Задача при этом обязана жить."""
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Написать пост", created_by="тест")
        sync_miro(store, state, miro, sprint_id=None)
        del miro.cards[miro.card_id("Написать пост")]
        result = sync_miro(store, state, miro, sprint_id=None)
        assert store.get_task(task.id).status == "open"
        assert any(change.kind == "unlinked" for change in result.changes)
        assert miro.has("Написать пост"), "карточка должна быть создана заново"
    finally:
        os.remove(db)


def test_rename_in_miro_reaches_us():
    db, store, state, miro = _setup()
    try:
        store.add_task("Задача с доски", created_by="тест")
        sync_miro(store, state, miro, sprint_id=None)
        miro.rename(miro.card_id("Задача с доски"), "Задача с доски (уточнил)")
        sync_miro(store, state, miro, sprint_id=None)
        assert any(t.title == "Задача с доски (уточнил)" for t in store.list_tasks())
    finally:
        os.remove(db)


def test_conflict_keeps_our_status_and_says_so():
    """Двигали и там, и тут: статус остаётся наш, но об этом сообщается.

    Молча потерянная правка хуже конфликта: про потерянную никто не узнает.
    """
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Спорная задача", created_by="тест")
        sync_miro(store, state, miro, sprint_id=None)
        miro.move(miro.card_id("Спорная задача"), "done")   # закрыли на доске
        store.claim_task(task.id, "Саша")                    # взяли в работу у нас
        result = sync_miro(store, state, miro, sprint_id=None)
        assert store.get_task(task.id).status != "done"
        assert any(change.kind == "conflict" for change in result.changes)
    finally:
        os.remove(db)


def test_miro_not_configured_is_not_an_error():
    """Нет токена — синхронизация просто выключена, остальное работает."""
    db, store, state, _ = _setup()
    try:
        store.add_task("Задача", created_by="тест")
        result = sync_miro(store, state, None, sprint_id=None)
        assert result.ok
        assert not result.changes
    finally:
        os.remove(db)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌   {test.__name__}: {exc}")
    print(f"\nвсего {len(tests)}, провалов {failed}")
    sys.exit(1 if failed else 0)
