from __future__ import annotations

import json
from pathlib import Path

# R-COST: тот же класс проблемы, что и с feedback_store.py — неограниченный
# список раздул бы промпт и разбавил остальные правила. Последних N постов
# достаточно, чтобы не повторяться в теме/шутке/структуре открытия.
_MAX_HISTORY_ENTRIES = 15


def _load(path: str) -> list[dict]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def record_published_post(path: str, *, category: str | None, summary: str, published_at: str) -> None:
    """Вызывать ТОЛЬКО в момент реальной публикации (не на черновик/ревью) —
    см. main.py: _publish_generated_post (автономный режим) и cb_approve_post
    (режим ревью)."""
    items = _load(path)
    items.append({"category": category, "summary": summary, "published_at": published_at})
    Path(path).write_text(json.dumps(items[-_MAX_HISTORY_ENTRIES:], ensure_ascii=False, indent=2), encoding="utf-8")


def load_recent_summaries(path: str, limit: int = 8) -> list[str]:
    items = _load(path)
    return [entry["summary"] for entry in items[-limit:] if entry.get("summary")]
