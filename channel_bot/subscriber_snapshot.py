from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def load_subscriber_snapshot(state_path: str) -> dict | None:
    """Последнее сохранённое число подписчиков (см. save_subscriber_snapshot)
    — источник для недельного прироста в посте "feedback_metrics". None,
    если снапшота ещё не было (первый запуск этой категории) — вызывающий
    код тогда не показывает изменение вообще, а не считает дельту от 0."""
    path = Path(state_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"count": int(data["count"]), "recorded_at": datetime.fromisoformat(data["recorded_at"])}
    except Exception:
        return None


def save_subscriber_snapshot(state_path: str, count: int, when: datetime) -> None:
    # Сохраняется ТОЛЬКО в момент реальной публикации поста "feedback_metrics"
    # (см. main.py/_snapshot_if_feedback_metrics), не в момент генерации
    # черновика/предпросмотра — иначе реролл/отклонённый черновик испортил бы
    # недельное сравнение для следующего реального понедельничного поста.
    Path(state_path).write_text(json.dumps({"count": count, "recorded_at": when.isoformat()}), encoding="utf-8")
