"""Two-way sync between Team Helper's SQLite tasks and the real Miro Table.

Unlike the old Miro integration (cards inside frames), the team's current board
is a Data Table with a Kanban view. The public Miro REST API cannot mutate that
format, but Miro's official MCP server exposes ``table_list_rows`` and
``table_sync_rows``. This module keeps SQLite as the source of truth *after*
the first bootstrap while still allowing people to create/edit tasks in Miro.

The first successful link is intentionally ``Miro wins`` for fields visible in
the table. That is a one-time migration rule: the team has been actively
planning in Miro while the old bot could not see this table. Once a row is
linked through ``task_sync``, normal conflict handling applies: if only one
side changed, that side wins; if both changed, SQLite wins and a conflict is
reported instead of silently dropping somebody's edit.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from shared import epics
from shared.miro_mcp_client import MiroMCPClient
from shared.sync_engine import Change, SyncResult, SyncState
from shared.task_store import Task, TaskNotFound, TaskStore

TARGET_MIRO_TABLE = "miro_table"
CHANGE_SOURCE = "miro"

STATUS_BACKLOG = "Бэклог"
STATUS_SPRINT = "Спринт"
STATUS_IN_PROGRESS = "В работе"
STATUS_TESTING = "На проверке"
STATUS_DONE = "Готово"

_PRIORITY_LABELS = {
    0: "P0 — критично",
    1: "P1 — высокий",
    2: "P2 — обычный",
    3: "P3 — низкий",
}

_TASK_ID_RE = re.compile(r"^\s*#(\d+)\s*(?:[·.\-:—]\s*)?(.*)$")


@dataclass(frozen=True)
class MiroTableRow:
    row_id: str
    display_title: str
    task_id: int | None
    title: str
    description: str
    priority: int
    estimate: float | None
    assignee: str | None
    tags: tuple[str, ...]
    status: str


class MiroTableBoard:
    """Synchronous adapter for one Miro Table item."""

    table_mode = True

    def __init__(
        self,
        *,
        table_url: str,
        auth_file: str,
        server_url: str = "https://mcp.miro.com/",
        redirect_uri: str = "http://127.0.0.1:8765/callback",
    ) -> None:
        self.table_url = table_url.strip()
        self._client = MiroMCPClient(
            auth_file=auth_file,
            server_url=server_url,
            redirect_uri=redirect_uri,
            interactive=False,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.table_url)

    def list_rows(self) -> list[MiroTableRow]:
        rows: list[MiroTableRow] = []
        cursor: str | None = None
        while True:
            args: dict[str, Any] = {"miro_url": self.table_url, "limit": 100}
            if cursor:
                args["next_cursor"] = cursor
            payload = _unwrap_payload(self._client.call_tool("table_list_rows", args))
            for raw in payload.get("rows") or []:
                if isinstance(raw, dict):
                    row = _parse_row(raw)
                    if row is not None:
                        rows.append(row)
            cursor = payload.get("next_cursor") or payload.get("cursor")
            if not cursor:
                return rows

    def sync_rows(self, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), 50):
            chunk = rows[start : start + 50]
            self._client.call_tool(
                "table_sync_rows",
                {"miro_url": self.table_url, "rows": chunk},
            )


class TaskWorkflowState:
    """Separates assignee from "work has started" without changing tasks schema.

    Historically ``claimed_by`` meant both things. Miro already models them as
    separate columns: Assignee may be Sasha while Status is still "Спринт 1".
    A tiny side table stores the missing bit. A SQLite trigger preserves the
    old Team Helper semantics: /claim (even by the already assigned person)
    marks work as started; /unclaim stops it. During Miro imports we apply the
    row status *after* assignee, so Miro's explicit Status wins.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_workflow_state (
                    task_id INTEGER PRIMARY KEY,
                    started INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS task_workflow_claim_updates
                AFTER UPDATE OF claimed_by ON tasks
                BEGIN
                    INSERT INTO task_workflow_state(task_id, started, updated_at)
                    VALUES (
                        NEW.id,
                        CASE WHEN NEW.claimed_by IS NULL THEN 0 ELSE 1 END,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                    ON CONFLICT(task_id) DO UPDATE SET
                        started = excluded.started,
                        updated_at = excluded.updated_at;
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

    def is_started(self, task: Task) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT started FROM task_workflow_state WHERE task_id = ?", (task.id,)
            ).fetchone()
        finally:
            conn.close()
        return bool(row["started"]) if row is not None else bool(task.claimed_by)

    def set_started(self, task_id: int, started: bool) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO task_workflow_state(task_id, started, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    started = excluded.started,
                    updated_at = excluded.updated_at
                """,
                (task_id, 1 if started else 0, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def board_from_env() -> MiroTableBoard | None:
    """Build table integration from env; empty MIRO_TABLE_URL disables it."""
    table_url = os.getenv("MIRO_TABLE_URL", "").strip()
    if not table_url:
        return None
    auth_file = os.getenv("MIRO_MCP_AUTH_FILE", ".miro_mcp_auth.json").strip()
    return MiroTableBoard(
        table_url=table_url,
        auth_file=auth_file,
        server_url=os.getenv("MIRO_MCP_SERVER_URL", "https://mcp.miro.com/").strip(),
        redirect_uri=os.getenv("MIRO_MCP_REDIRECT_URI", "http://127.0.0.1:8765/callback").strip(),
    )


def sync_miro_table(
    store: TaskStore,
    state: SyncState,
    board: MiroTableBoard | None,
    *,
    sprint_id: int | None,
) -> SyncResult:
    result = SyncResult()
    if board is None or not board.enabled:
        return result

    workflow = TaskWorkflowState(_db_path(store))
    try:
        remote_rows = board.list_rows()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Miro Table: {exc}")
        return result

    known = state.external_ids(TARGET_MIRO_TABLE)
    seen_task_ids: set[int] = set()

    for row in remote_rows:
        task_id = known.get(row.row_id) or row.task_id
        if task_id is not None and task_id in seen_task_ids:
            result.errors.append(
                f"Miro Table: две строки ссылаются на #{task_id}; оставил первую, дубль rowId={row.row_id}"
            )
            continue

        if task_id is None:
            if not row.title:
                continue
            task = store.add_task(
                row.title,
                created_by="miro",
                description=row.description,
                epic=_epic_from_tags(row.tags, row.title),
                priority=row.priority,
                origin=CHANGE_SOURCE,
                estimate_hours=row.estimate,
            )
            _apply_remote_fields(store, workflow, task, row, sprint_id=sprint_id)
            task = store.get_task(task.id)
            store.set_miro_item_id(task.id, row.row_id)
            desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
            try:
                board.sync_rows([_row_update(row.row_id, desired)])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Miro Table #{task.id}: {exc}")
            state.remember(
                task.id,
                TARGET_MIRO_TABLE,
                external_id=row.row_id,
                snapshot=_snapshot(
                    _external_fingerprint_values(desired),
                    _local_fingerprint(task, workflow),
                ),
            )
            result.changes.append(
                Change(task.id, task.title, "created", "создана строкой в Miro", CHANGE_SOURCE)
            )
            seen_task_ids.add(task.id)
            continue

        try:
            task = store.get_task(task_id)
        except TaskNotFound:
            task = _import_exact_id(store, row, task_id)
            result.changes.append(
                Change(task.id, task.title, "created", "импортирована из Miro с сохранением #ID", CHANGE_SOURCE)
            )

        seen_task_ids.add(task.id)
        external_id, snapshot, _ = state.get(task.id, TARGET_MIRO_TABLE)
        task = _normalize_local_sprint(store, workflow, task, sprint_id)

        if external_id != row.row_id:
            store.set_miro_item_id(task.id, row.row_id)

        if snapshot is None or external_id != row.row_id:
            _apply_remote_fields(store, workflow, task, row, sprint_id=sprint_id)
            task = store.get_task(task.id)
            desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
            try:
                if _external_fingerprint(row) != _external_fingerprint_values(desired):
                    board.sync_rows([_row_update(row.row_id, desired)])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Miro Table #{task.id}: {exc}")
            state.remember(
                task.id,
                TARGET_MIRO_TABLE,
                external_id=row.row_id,
                snapshot=_snapshot(
                    _external_fingerprint_values(desired),
                    _local_fingerprint(task, workflow),
                ),
            )
            continue

        external_seen, local_seen = _split_snapshot(snapshot)
        remote_fp = _external_fingerprint(row)
        local_fp = _local_fingerprint(task, workflow)
        remote_changed = remote_fp != external_seen
        local_changed = local_fp != local_seen

        if remote_changed and local_changed:
            desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
            try:
                board.sync_rows([_row_update(row.row_id, desired)])
                state.remember(
                    task.id,
                    TARGET_MIRO_TABLE,
                    external_id=row.row_id,
                    snapshot=_snapshot(_external_fingerprint_values(desired), local_fp),
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Miro Table #{task.id}: {exc}")
            result.changes.append(
                Change(
                    task.id,
                    task.title,
                    "conflict",
                    "задачу меняли одновременно в Miro и Team Helper — оставил версию Team Helper",
                    CHANGE_SOURCE,
                    task.claimed_by_user_id,
                )
            )
            continue

        if remote_changed:
            before_status = _status_for_task(task, workflow, sprint_label=_sprint_label())
            before_title = task.title
            _apply_remote_fields(store, workflow, task, row, sprint_id=sprint_id)
            task = store.get_task(task.id)
            desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
            try:
                if _external_fingerprint(row) != _external_fingerprint_values(desired):
                    board.sync_rows([_row_update(row.row_id, desired)])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Miro Table #{task.id}: {exc}")
            state.remember(
                task.id,
                TARGET_MIRO_TABLE,
                external_id=row.row_id,
                snapshot=_snapshot(
                    _external_fingerprint_values(desired),
                    _local_fingerprint(task, workflow),
                ),
            )
            after_status = _status_for_task(task, workflow, sprint_label=_sprint_label())
            kind = "status" if after_status != before_status else ("renamed" if task.title != before_title else "updated")
            result.changes.append(
                Change(task.id, task.title, kind, "изменена в Miro", CHANGE_SOURCE, task.claimed_by_user_id)
            )
            continue

        if local_changed:
            desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
            try:
                board.sync_rows([_row_update(row.row_id, desired)])
                state.remember(
                    task.id,
                    TARGET_MIRO_TABLE,
                    external_id=row.row_id,
                    snapshot=_snapshot(_external_fingerprint_values(desired), local_fp),
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Miro Table #{task.id}: {exc}")

    local_tasks = store.list_tasks(include_done=True)
    remote_ids = {row.row_id for row in remote_rows}
    rows_to_create: list[dict[str, Any]] = []
    created_task_ids: list[int] = []

    for task in local_tasks:
        task = _normalize_local_sprint(store, workflow, task, sprint_id)
        external_id, _, _ = state.get(task.id, TARGET_MIRO_TABLE)
        if external_id and external_id in remote_ids:
            continue
        if task.id in seen_task_ids:
            continue

        if external_id and external_id not in remote_ids:
            state.forget(task.id, TARGET_MIRO_TABLE)
            result.changes.append(
                Change(
                    task.id,
                    task.title,
                    "unlinked",
                    "строка в Miro исчезла — задача сохранена, строку создам заново",
                    CHANGE_SOURCE,
                    task.claimed_by_user_id,
                )
            )

        desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=None)
        rows_to_create.append(_row_insert(desired))
        created_task_ids.append(task.id)

    if rows_to_create:
        try:
            board.sync_rows(rows_to_create)
            refreshed = board.list_rows()
            by_task_id = {r.task_id: r for r in refreshed if r.task_id is not None}
            for task_id in created_task_ids:
                row = by_task_id.get(task_id)
                if row is None:
                    result.errors.append(f"Miro Table #{task_id}: строка создана, но rowId не удалось найти")
                    continue
                task = store.get_task(task_id)
                store.set_miro_item_id(task.id, row.row_id)
                desired = _desired_row(task, workflow, sprint_label=_sprint_label(), preserve_tags=row.tags)
                state.remember(
                    task.id,
                    TARGET_MIRO_TABLE,
                    external_id=row.row_id,
                    snapshot=_snapshot(
                        _external_fingerprint_values(desired),
                        _local_fingerprint(task, workflow),
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"Miro Table: не удалось создать строки: {exc}")

    return result


def _apply_remote_fields(
    store: TaskStore,
    workflow: TaskWorkflowState,
    task: Task,
    row: MiroTableRow,
    *,
    sprint_id: int | None,
) -> None:
    if row.title and row.title != task.title:
        task = store.rename_task(task.id, row.title)
    if (task.description or "") != row.description:
        task = store.set_description(task.id, row.description)
    if task.priority != row.priority:
        task = store.set_priority(task.id, row.priority)
    if task.estimate_hours != row.estimate:
        task = store.set_estimate(task.id, row.estimate)

    remote_epic = _epic_from_tags(row.tags, row.title, allow_fallback=False)
    if row.tags and remote_epic != task.epic:
        task = store.set_epic(task.id, remote_epic)

    assignee = row.assignee.strip() if row.assignee else None
    if assignee != task.claimed_by:
        if assignee:
            task = store.claim_task(task.id, assignee, None)
        else:
            task = store.unclaim_task(task.id)

    _apply_remote_status(store, workflow, task.id, row.status, sprint_id=sprint_id)


def _apply_remote_status(
    store: TaskStore,
    workflow: TaskWorkflowState,
    task_id: int,
    status: str,
    *,
    sprint_id: int | None,
) -> None:
    task = store.get_task(task_id)
    normalized = _normalize_remote_status(status)

    if normalized == STATUS_DONE:
        if task.status != "done":
            store.complete_task(task_id)
        workflow.set_started(task_id, False)
        return

    if task.status in ("done", "testing", "cancelled"):
        task = store.reopen_task(task_id)

    if normalized == STATUS_BACKLOG:
        if task.sprint_id is not None:
            store.set_sprint(task_id, None)
        workflow.set_started(task_id, False)
        return

    if normalized == STATUS_SPRINT:
        if sprint_id is not None and task.sprint_id != sprint_id:
            store.set_sprint(task_id, sprint_id)
        workflow.set_started(task_id, False)
        return

    if normalized == STATUS_IN_PROGRESS:
        if sprint_id is not None and task.sprint_id != sprint_id:
            store.set_sprint(task_id, sprint_id)
        workflow.set_started(task_id, True)
        return

    if normalized == STATUS_TESTING:
        if sprint_id is not None and task.sprint_id != sprint_id:
            store.set_sprint(task_id, sprint_id)
        store.mark_testing(task_id)
        workflow.set_started(task_id, True)


def _import_exact_id(store: TaskStore, row: MiroTableRow, task_id: int) -> Task:
    now = _now()
    status = "done" if _normalize_remote_status(row.status) == STATUS_DONE else "open"
    completed_at = now if status == "done" else None
    with store._connect() as db:  # noqa: SLF001 - migration-only path
        db.execute(
            """
            INSERT INTO tasks (
                id, title, status, claimed_by, created_by, created_at,
                completed_at, description, epic, priority, estimate_hours,
                updated_at, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                row.title or f"Задача {task_id}",
                status,
                row.assignee,
                "miro",
                now,
                completed_at,
                row.description or None,
                _epic_from_tags(row.tags, row.title),
                row.priority,
                row.estimate,
                now,
                CHANGE_SOURCE,
            ),
        )
    return store.get_task(task_id)


def _normalize_local_sprint(
    store: TaskStore,
    workflow: TaskWorkflowState,
    task: Task,
    sprint_id: int | None,
) -> Task:
    if (
        sprint_id is not None
        and task.status not in ("done", "cancelled")
        and task.sprint_id is None
        and workflow.is_started(task)
    ):
        return store.set_sprint(task.id, sprint_id)
    return task


def _desired_row(
    task: Task,
    workflow: TaskWorkflowState,
    *,
    sprint_label: str,
    preserve_tags: Iterable[str] | None,
) -> dict[str, Any]:
    tags = list(preserve_tags or ())
    if preserve_tags is None and task.epic and task.epic in epics.all_codes():
        tags = [task.epic]
    return {
        "Title": f"#{task.id} · {task.title}",
        "Description": task.description or "",
        "Priority": _PRIORITY_LABELS.get(task.priority, _PRIORITY_LABELS[2]),
        "Estimate": task.estimate_hours,
        "Assignee": task.claimed_by or "",
        "Tags": tags,
        "Status": _status_for_task(task, workflow, sprint_label=sprint_label),
    }


def _status_for_task(task: Task, workflow: TaskWorkflowState, *, sprint_label: str) -> str:
    if task.status == "done":
        return STATUS_DONE
    if task.status == "testing":
        return STATUS_TESTING
    if workflow.is_started(task):
        return STATUS_IN_PROGRESS
    if task.sprint_id is not None:
        return sprint_label
    return STATUS_BACKLOG


def _row_insert(values: dict[str, Any]) -> dict[str, Any]:
    return {"cells": _cells(values, include_tags=True)}


def _row_update(row_id: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"rowId": row_id, "cells": _cells(values, include_tags=False)}


def _cells(values: dict[str, Any], *, include_tags: bool) -> list[dict[str, Any]]:
    cells = [
        {"columnTitle": "Title", "value": values["Title"]},
        {"columnTitle": "Description", "value": values["Description"]},
        {"columnTitle": "Priority", "value": values["Priority"]},
        {"columnTitle": "Estimate", "value": values["Estimate"] if values["Estimate"] is not None else ""},
        {"columnTitle": "Assignee", "value": values["Assignee"]},
        {"columnTitle": "Status", "value": values["Status"]},
    ]
    if include_tags and values.get("Tags"):
        cells.append({"columnTitle": "Tags", "value": list(values["Tags"])})
    return cells


def _parse_row(raw: dict[str, Any]) -> MiroTableRow | None:
    row_id = str(raw.get("rowId") or raw.get("row_id") or "").strip()
    if not row_id:
        return None
    values: dict[str, Any] = {}
    for cell in raw.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        title = str(cell.get("columnTitle") or "").strip()
        if not title:
            continue
        if "content" in cell:
            values[title] = cell.get("content") or ""
        elif "quantity" in cell:
            values[title] = cell.get("quantity")
        elif "options" in cell:
            options = cell.get("options") or []
            values[title] = [
                str(x.get("displayValue"))
                for x in options
                if isinstance(x, dict) and x.get("displayValue")
            ]
        elif "value" in cell:
            values[title] = cell.get("value")

    display_title = str(values.get("Title") or "").strip()
    task_id, title = _split_title(display_title)
    tags_raw = values.get("Tags") or []
    tags = (tags_raw,) if isinstance(tags_raw, str) else tuple(str(x) for x in tags_raw if x)

    return MiroTableRow(
        row_id=row_id,
        display_title=display_title,
        task_id=task_id,
        title=title,
        description=str(values.get("Description") or ""),
        priority=_priority_from_value(values.get("Priority")),
        estimate=_float_or_none(values.get("Estimate")),
        assignee=str(values.get("Assignee") or "").strip() or None,
        tags=tags,
        status=_select_value(values.get("Status")) or STATUS_BACKLOG,
    )


def _unwrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    current: Any = payload
    for _ in range(3):
        if not isinstance(current, dict):
            break
        if "rows" in current:
            return current
        if isinstance(current.get("result"), dict):
            current = current["result"]
            continue
        if isinstance(current.get("data"), dict):
            current = current["data"]
            continue
        break
    return current if isinstance(current, dict) else {}


def _split_title(display_title: str) -> tuple[int | None, str]:
    match = _TASK_ID_RE.match(display_title)
    if not match:
        return None, display_title.strip()
    task_id = int(match.group(1))
    title = (match.group(2) or "").strip()
    return task_id, title or f"Задача {task_id}"


def _priority_from_value(value: Any) -> int:
    selected = _select_value(value)
    match = re.search(r"P([0-3])", selected, re.IGNORECASE)
    return int(match.group(1)) if match else 2


def _select_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_remote_status(value: str) -> str:
    value = (value or "").strip()
    if value == STATUS_BACKLOG:
        return STATUS_BACKLOG
    if value in {STATUS_SPRINT, "Спринт 1", _sprint_label()}:
        return STATUS_SPRINT
    if value == STATUS_IN_PROGRESS:
        return STATUS_IN_PROGRESS
    if value in {STATUS_TESTING, "Тестируется"}:
        return STATUS_TESTING
    if value == STATUS_DONE:
        return STATUS_DONE
    return STATUS_BACKLOG


def _sprint_label() -> str:
    return os.getenv("MIRO_TABLE_SPRINT_STATUS", "Спринт 1").strip() or "Спринт 1"


def _epic_from_tags(tags: Iterable[str], title: str, *, allow_fallback: bool = True) -> str | None:
    valid = set(epics.all_codes())
    for tag in tags:
        code = str(tag).strip().lower()
        if code in valid:
            return code
    return (epics.classify_by_keywords(title) or None) if allow_fallback else None


def _external_fingerprint(row: MiroTableRow) -> str:
    return _digest(
        "|".join(
            [
                row.display_title,
                row.description,
                str(row.priority),
                str(row.estimate),
                row.assignee or "",
                ",".join(row.tags),
                row.status,
            ]
        )
    )


def _external_fingerprint_values(values: dict[str, Any]) -> str:
    return _digest(
        "|".join(
            [
                str(values.get("Title") or ""),
                str(values.get("Description") or ""),
                str(_priority_from_value(values.get("Priority"))),
                str(values.get("Estimate")),
                str(values.get("Assignee") or ""),
                ",".join(str(x) for x in values.get("Tags") or []),
                str(values.get("Status") or ""),
            ]
        )
    )


def _local_fingerprint(task: Task, workflow: TaskWorkflowState) -> str:
    return _digest(
        "|".join(
            [
                task.title,
                task.description or "",
                str(task.priority),
                str(task.estimate_hours),
                task.claimed_by or "",
                task.epic or "",
                task.status,
                str(task.sprint_id),
                "1" if workflow.is_started(task) else "0",
            ]
        )
    )


def _snapshot(external: str, local: str) -> str:
    return f"{external}:{local}"


def _split_snapshot(snapshot: str | None) -> tuple[str | None, str | None]:
    if not snapshot or ":" not in snapshot:
        return None, None
    external, local = snapshot.split(":", 1)
    return external or None, local or None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _db_path(store: TaskStore) -> str:
    path = getattr(store, "_db_path", None)
    if not path:
        raise RuntimeError("TaskStore не exposes db_path для workflow sync")
    return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
