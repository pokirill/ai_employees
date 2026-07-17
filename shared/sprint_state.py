from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Общий модуль, а не приватные функции внутри team_bot/main.py — webapp/
# server.py тоже должен показывать текущий период спринта на доске, а эти
# два процесса — независимо деплоящиеся (см. shared/config.py:
# TaskBoardConfig), не должны импортировать друг друга.


def load_last_sprint_at(path: str) -> datetime | None:
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(file_path.read_text(encoding="utf-8"))["sent_at"])
    except Exception:
        return None


def save_last_sprint_at(path: str, when: datetime) -> None:
    Path(path).write_text(json.dumps({"sent_at": when.isoformat()}), encoding="utf-8")


def current_sprint_period(path: str) -> tuple[datetime, datetime]:
    """(since, now) — since — момент окончания ПРЕДЫДУЩЕГО спринта (UTC-aware,
    т.к. TaskStore хранит completed_at/cancelled_at в UTC), первый раз — 7
    дней назад. Только ЧТЕНИЕ границы — продвигать её (save_last_sprint_at)
    должен только настоящий еженедельный цикл в team_bot, не webapp
    (иначе открытие доски "закрывало" бы спринт как побочный эффект)."""
    since = load_last_sprint_at(path) or (datetime.now(timezone.utc) - timedelta(days=7))
    now = datetime.now(timezone.utc)
    return since, now
