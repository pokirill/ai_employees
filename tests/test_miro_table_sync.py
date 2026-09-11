"""Miro Table sync regression tests.

No network/OAuth: FakeMiroTable implements the two operations the engine uses.
These tests protect the migration rules that are dangerous to get wrong on the
real 156-row board: preserving #IDs, keeping Assignee separate from 'started',
idempotency, and never deleting core tasks because a Miro row disappeared.
"""

from __future__ import annotations

import os
import tempfile

from shared.miro_table_sync import (
    STATUS_IN_PROGRESS,
    STATUS_TESTING,
    MiroTableRow,
    TaskWorkflowState,
    sync_miro_table,
)
from shared.sync_engine import SyncState
from shared.task_store import TaskStore


class FakeMiroTable:
    enabled = True
    table_mode = True

    def __init__(self, rows=None):
        self.rows: dict[str, MiroTableRow] = {r.row_id: r for r in (rows or [])}
        self._next = 1
        self.calls = 0

    def list_rows(self):
        return list(self.rows.values())

    def sync_rows(self, patches):
        self.calls += 1
        for patch in patches:
            row_id = patch.get("rowId")
            values = {cell["columnTitle"]: cell.get("value") for cell in patch["cells"]}
            if row_id is None:
                row_id = f"row-new-{self._next}"
                self._next += 1
                current = _row(row_id, "")
            else:
                current = self.rows[row_id]
            self.rows[row_id] = _merge(current, values)

    def row_for_task(self, task_id):
        return next(r for r in self.rows.values() if r.task_id == task_id)


def _row(
    row_id,
    title,
    *,
    status="Бэклог",
    assignee=None,
    description="",
    priority=2,
    estimate=None,
    tags=(),
):
    display = title
    task_id = None
    clean = title
    if title.startswith("#") and "·" in title:
        head, clean = title.split("·", 1)
        task_id = int(head.strip().lstrip("#"))
        clean = clean.strip()
    return MiroTableRow(
        row_id=row_id,
        display_title=display,
        task_id=task_id,
        title=clean,
        description=description,
        priority=priority,
        estimate=estimate,
        assignee=assignee,
        tags=tuple(tags),
        status=status,
    )


def _merge(row, values):
    display = values.get("Title", row.display_title)
    task_id = row.task_id
    title = row.title
    if display.startswith("#") and "·" in display:
        head, title = display.split("·", 1)
        task_id = int(head.strip().lstrip("#"))
        title = title.strip()
    priority_text = values.get("Priority")
    priority = row.priority
    if priority_text:
        priority = int(str(priority_text)[1])
    estimate = values.get("Estimate", row.estimate)
    if estimate == "":
        estimate = None
    return MiroTableRow(
        row_id=row.row_id,
        display_title=display,
        task_id=task_id,
        title=title,
        description=values.get("Description", row.description),
        priority=priority,
        estimate=estimate,
        assignee=values.get("Assignee", row.assignee) or None,
        tags=row.tags,
        status=values.get("Status", row.status),
    )


def _setup(rows=None):
    db = tempfile.mktemp(suffix=".db")
    return db, TaskStore(db), SyncState(db), FakeMiroTable(rows)


def test_bootstrap_preserves_existing_id_and_assignee_is_not_started(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    row = _row(
        "row-157",
        "#157 · Первая реальная трата",
        status="Спринт 1",
        assignee="Саша",
        priority=0,
        tags=("product", "research"),
    )
    db, store, state, miro = _setup([row])
    try:
        result = sync_miro_table(store, state, miro, sprint_id=1)
        assert not result.errors, result.errors
        task = store.get_task(157)
        assert task.claimed_by == "Саша"
        assert task.sprint_id == 1
        assert TaskWorkflowState(db).is_started(task) is False
        assert miro.row_for_task(157).status == "Спринт 1"
    finally:
        os.remove(db)


def test_new_miro_row_gets_core_id_and_canonical_title(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    db, store, state, miro = _setup([_row("row-x", "Новая задача", status="Бэклог")])
    try:
        result = sync_miro_table(store, state, miro, sprint_id=1)
        assert not result.errors, result.errors
        tasks = store.list_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert miro.rows["row-x"].display_title == f"#{task.id} · Новая задача"
        assert state.get(task.id, "miro_table")[0] == "row-x"
    finally:
        os.remove(db)


def test_local_task_is_created_in_miro_and_second_pass_is_idempotent(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Из бота", created_by="test")
        first = sync_miro_table(store, state, miro, sprint_id=1)
        assert not first.errors, first.errors
        assert miro.row_for_task(task.id).title == "Из бота"
        calls = miro.calls
        second = sync_miro_table(store, state, miro, sprint_id=1)
        assert not second.errors, second.errors
        assert miro.calls == calls
        assert len(miro.rows) == 1
    finally:
        os.remove(db)


def test_claim_marks_started_and_moves_to_in_progress(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Начать", created_by="test")
        store.set_sprint(task.id, 1)
        sync_miro_table(store, state, miro, sprint_id=1)
        store.claim_task(task.id, "Саша", 42)
        sync_miro_table(store, state, miro, sprint_id=1)
        assert miro.row_for_task(task.id).status == STATUS_IN_PROGRESS
        assert miro.row_for_task(task.id).assignee == "Саша"
    finally:
        os.remove(db)


def test_miro_testing_updates_core_status(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Проверить", created_by="test")
        store.set_sprint(task.id, 1)
        sync_miro_table(store, state, miro, sprint_id=1)
        row = miro.row_for_task(task.id)
        miro.rows[row.row_id] = _row(
            row.row_id,
            row.display_title,
            status=STATUS_TESTING,
            assignee="Саша",
        )
        result = sync_miro_table(store, state, miro, sprint_id=1)
        assert not result.errors, result.errors
        assert store.get_task(task.id).status == "testing"
    finally:
        os.remove(db)


def test_deleted_miro_row_does_not_delete_task(monkeypatch):
    monkeypatch.setenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1")
    db, store, state, miro = _setup()
    try:
        task = store.add_task("Не потерять", created_by="test")
        sync_miro_table(store, state, miro, sprint_id=1)
        row = miro.row_for_task(task.id)
        del miro.rows[row.row_id]
        result = sync_miro_table(store, state, miro, sprint_id=1)
        assert store.get_task(task.id).title == "Не потерять"
        assert miro.row_for_task(task.id).title == "Не потерять"
        assert any(change.kind == "unlinked" for change in result.changes)
    finally:
        os.remove(db)
