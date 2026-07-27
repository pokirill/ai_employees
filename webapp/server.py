from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shared.config import TaskBoardConfig
from shared.sprint_state import current_sprint_period
from shared.task_store import Task, TaskNotFound, TaskStore
from shared.telegram_webapp_auth import InvalidInitData, validate_init_data

_STATIC_DIR = Path(__file__).parent / "static"

_board_config = TaskBoardConfig()
# Не тянем весь TeamBotConfig — он требует iCloud-пароль/TEAM_CHAT_ID/путь к
# докам, которые webapp вообще не использует. Мини-апп может деплоиться
# отдельно от team_bot, ему нужен только сам токен для проверки initData.
_bot_token = os.getenv("TEAM_BOT_TOKEN")
if not _bot_token:
    raise RuntimeError("Missing required env var: TEAM_BOT_TOKEN (нужен для проверки initData мини-аппа)")
_store = TaskStore(_board_config.db_path)

app = FastAPI(title="Кубышка — доска задач")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


_DEFAULT_DONE_VISIBLE_DAYS = 7


class InitDataPayload(BaseModel):
    init_data: str


class ListPayload(InitDataPayload):
    # False (по умолчанию) → готовые задачи старше _DEFAULT_DONE_VISIBLE_DAYS
    # скрыты — доска не должна расти в бесконечную ленту навсегда. True —
    # показать всю историю (переключатель в UI).
    include_all: bool = False


class NewTaskPayload(InitDataPayload):
    title: str
    description: str = ""


class CommentPayload(InitDataPayload):
    text: str


class DescriptionPayload(InitDataPayload):
    description: str


class RenamePayload(InitDataPayload):
    title: str


def _authenticated_user(init_data: str) -> dict:
    # initData подписана Telegram HMAC-ом на секрете токена бота — без этой
    # проверки любой человек с ссылкой на webapp мог бы дёргать API от чужого
    # имени (см. shared/telegram_webapp_auth.py).
    try:
        data = validate_init_data(init_data, _bot_token)
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return data.get("user", {})


def _display_name(user: dict) -> str:
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return name or user.get("username") or f"id{user.get('id', '?')}"


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "claimed_by": task.claimed_by,
        "claimed_by_user_id": task.claimed_by_user_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "cancelled_at": task.cancelled_at,
        "comments": [{"author": c.author, "text": c.text, "created_at": c.created_at} for c in task.comments],
        "photos": [
            {
                "url": f"/static/task_photos/{p.file_name}",
                "caption": p.caption,
                "added_by": p.added_by,
                "created_at": p.created_at,
            }
            for p in task.photos
        ],
    }


@app.post("/api/tasks/list")
def list_tasks(payload: ListPayload) -> list[dict]:
    _authenticated_user(payload.init_data)
    done_within_days = None if payload.include_all else _DEFAULT_DONE_VISIBLE_DAYS
    return [_task_to_dict(t) for t in _store.list_tasks(done_within_days=done_within_days)]


@app.post("/api/tasks/create")
def create_task(payload: NewTaskPayload) -> dict:
    user = _authenticated_user(payload.init_data)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Пустое название задачи")
    task = _store.add_task(title, created_by=_display_name(user), description=payload.description.strip())
    return _task_to_dict(task)


@app.post("/api/tasks/{task_id}/claim")
def claim_task(task_id: int, payload: InitDataPayload) -> dict:
    user = _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.claim_task(task_id, _display_name(user), user.get("id")))


@app.post("/api/tasks/{task_id}/testing")
def mark_testing(task_id: int, payload: InitDataPayload) -> dict:
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.mark_testing(task_id))


@app.post("/api/tasks/{task_id}/description")
def set_description(task_id: int, payload: DescriptionPayload) -> dict:
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.set_description(task_id, payload.description.strip()))


@app.post("/api/tasks/{task_id}/rename")
def rename_task(task_id: int, payload: RenamePayload) -> dict:
    _authenticated_user(payload.init_data)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Пустое название задачи")
    return _with_404(lambda: _store.rename_task(task_id, title))


@app.post("/api/tasks/{task_id}/unclaim")
def unclaim_task(task_id: int, payload: InitDataPayload) -> dict:
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.unclaim_task(task_id))


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: int, payload: InitDataPayload) -> dict:
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.complete_task(task_id))


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int, payload: InitDataPayload) -> dict:
    # Отдельно от complete — "отменили" и "сделали" разные исходы для
    # недельного спринт-дайджеста (см. shared/sprint_digest.py).
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.cancel_task(task_id))


@app.post("/api/tasks/{task_id}/reopen")
def reopen_task(task_id: int, payload: InitDataPayload) -> dict:
    _authenticated_user(payload.init_data)
    return _with_404(lambda: _store.reopen_task(task_id))


@app.post("/api/sprint")
def sprint_status(payload: InitDataPayload) -> dict:
    # Только ЧТЕНИЕ текущего периода — границу продвигает исключительно
    # реальный еженедельный цикл в team_bot (см. shared/sprint_state.py),
    # открытие доски не должно "закрывать" спринт как побочный эффект.
    _authenticated_user(payload.init_data)
    since, now = current_sprint_period(_board_config.sprint_state_path)
    return {
        "period_label": f"{since:%d.%m}–{now:%d.%m}",
        "done_count": len(_store.list_done_since(since)),
        "cancelled_count": len(_store.list_cancelled_since(since)),
        "still_open_count": len(_store.list_tasks(include_done=False)),
    }


@app.post("/api/tasks/{task_id}/comment")
def comment_task(task_id: int, payload: CommentPayload) -> dict:
    user = _authenticated_user(payload.init_data)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой комментарий")
    return _with_404(lambda: _store.add_comment(task_id, _display_name(user), text))


def _with_404(action) -> dict:
    try:
        return _task_to_dict(action())
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_board_config.webapp_port)
