from __future__ import annotations

import json
from pathlib import Path

# Формат файла очереди — JSON-массив строк. Команда просто дописывает темы
# в конец через редактирование файла (или отдельный /addtopic — см. main.py).


def load_queue(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def save_queue(path: str, items: list[str]) -> None:
    Path(path).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_next_topic(path: str) -> str | None:
    items = load_queue(path)
    if not items:
        return None
    topic = items.pop(0)
    save_queue(path, items)
    return topic


def peek_next_topic(path: str) -> str | None:
    """Как pop_next_topic, но не убирает тему из очереди — для /preview,
    чтобы предпросмотр не "тратил" реальную тему из очереди."""
    items = load_queue(path)
    return items[0] if items else None


def append_topic(path: str, topic: str) -> None:
    items = load_queue(path)
    items.append(topic)
    save_queue(path, items)
