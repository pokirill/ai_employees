from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# R-COST: пассивный буфер сообщений чата команды — ноль вызовов LLM на
# запись, ноль на каждое сообщение. Читается ЦЕЛИКОМ раз в неделю в
# sprint_loop как доп. контекст для оценки эффективности/планирования (см.
# team_bot/main.py) — единственное место, где это реально стоит токенов.
_MAX_ENTRIES = 400


def _load(path: str) -> list[dict]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def append_chat_message(path: str, *, author: str, text: str) -> None:
    items = _load(path)
    items.append({"author": author, "text": text, "at": datetime.now(timezone.utc).isoformat()})
    Path(path).write_text(json.dumps(items[-_MAX_ENTRIES:], ensure_ascii=False, indent=2), encoding="utf-8")


def messages_since(path: str, since: datetime) -> list[dict]:
    items = _load(path)
    result = []
    for item in items:
        try:
            at = datetime.fromisoformat(item["at"])
        except Exception:
            continue
        if at >= since:
            result.append(item)
    return result


def format_for_prompt(messages: list[dict], max_chars: int = 4000) -> str:
    """Свежие сообщения важнее старых — если не влезает всё, обрезаем
    старый хвост, а не конец (последние строки самые актуальные)."""
    lines = [f"{m['author']}: {m['text']}" for m in messages]
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text
