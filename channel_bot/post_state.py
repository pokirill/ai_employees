from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_last_post_at(state_path: str) -> datetime | None:
    path = Path(state_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_post_at"])
    except Exception:
        return None


def save_last_post_at(state_path: str, when: datetime, title: str = "") -> None:
    Path(state_path).write_text(
        json.dumps({"last_post_at": when.isoformat(), "last_post_title": title}), encoding="utf-8"
    )


def load_last_post_info(state_path: str) -> dict | None:
    """Как load_last_post_at, но вместе с заголовком последнего поста — для
    /status, чтобы показать не только "когда", но и "что" постилось."""
    path = Path(state_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"last_post_at": datetime.fromisoformat(data["last_post_at"]), "last_post_title": data.get("last_post_title", "")}
    except Exception:
        return None


def seconds_until_next_post(state_path: str, interval_hours: int) -> float:
    """0, если пора постить сейчас (или ещё ни разу не постили — не спамим
    сразу при первом запуске, но и не молчим сутки, если это первый пост
    когда-либо: см. отдельную обработку в caller при last_post_at is None)."""
    last = load_last_post_at(state_path)
    if last is None:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    remaining = interval_hours * 3600 - elapsed
    return max(0.0, remaining)
