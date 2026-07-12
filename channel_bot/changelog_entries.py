from __future__ import annotations

import json
import re
from pathlib import Path

_ENTRY_HEADER = re.compile(r"^- \*\*\[(.+?)\]\*\*\s*$", re.MULTILINE)


def parse_changelog_entries(changelog_path: str) -> list[dict[str, str]]:
    """Разбивает AI_CHANGELOG.md на записи (заголовок + тело до следующего
    заголовка/пустой строки+заголовка). Возвращает newest-first (порядок
    файла — конвенция этого репо: свежее сверху)."""
    path = Path(changelog_path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")

    matches = list(_ENTRY_HEADER.finditer(text))
    entries: list[dict[str, str]] = []
    for i, match in enumerate(matches):
        title = match.group(1)
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        entries.append({"title": title, "body": body})
    return entries


def load_used_titles(state_path: str) -> set[str]:
    path = Path(state_path)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def mark_title_used(state_path: str, title: str) -> None:
    used = load_used_titles(state_path)
    used.add(title)
    Path(state_path).write_text(json.dumps(sorted(used), ensure_ascii=False, indent=2), encoding="utf-8")


def next_unused_entry(changelog_path: str, state_path: str) -> dict[str, str] | None:
    used = load_used_titles(state_path)
    for entry in parse_changelog_entries(changelog_path):
        if entry["title"] not in used:
            return entry
    return None
