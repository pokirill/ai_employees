from __future__ import annotations

import json
from pathlib import Path

# R-COST: без потолка список замечаний рос бы бесконечно и размывал бы
# остальные правила промпта (тот же класс проблемы, что уже дважды ловили в
# _SYSTEM_PROMPT — чем больше правил, тем менее надёжно соблюдается каждое).
# Старые записи вытесняются новыми — считаем, что либо уже усвоены моделью
# через сам факт присутствия в истории постов, либо неактуальны.
_MAX_FEEDBACK_ENTRIES = 12


def load_feedback(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _save_feedback(path: str, items: list[str]) -> None:
    Path(path).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def add_feedback(path: str, text: str) -> None:
    items = load_feedback(path)
    items.append(text)
    _save_feedback(path, items[-_MAX_FEEDBACK_ENTRIES:])


def remove_feedback(path: str, index: int) -> str | None:
    """index — 0-based. Возвращает удалённый текст или None, если индекс вне диапазона."""
    items = load_feedback(path)
    if not (0 <= index < len(items)):
        return None
    removed = items.pop(index)
    _save_feedback(path, items)
    return removed
