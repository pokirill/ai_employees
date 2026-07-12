from __future__ import annotations

import subprocess
from pathlib import Path

# Файлы, которые реально пригодны как контекст ассистента/источник тем —
# не весь Docs/, там есть многотысячестрочные исторические логи.
_DEFAULT_CONTEXT_FILES = [
    "BACKLOG.md",
    "AI_CHANGELOG.md",
    "RELEASE_NOTES.md",
]

# Сколько последних строк AI_CHANGELOG.md брать — файл растёт неограниченно,
# и только «голова» (самые свежие записи) релевантна для контекста.
_CHANGELOG_TAIL_LINES = 400


def sync_docs_repo(docs_path: str) -> None:
    """git pull, если docs_path — рабочая копия git-репозитория. Тихо
    пропускает, если это не git или сеть недоступна — старые доки лучше,
    чем упавший бот."""
    repo_root = Path(docs_path).parent
    if not (repo_root / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def load_project_context(docs_path: str, max_chars: int = 12_000) -> str:
    """Собирает контекст проекта из Docs/*.md FinAssist для промпта LLM.
    AI_CHANGELOG.md обрезается до последних строк (самые свежие записи —
    в начале файла по конвенции этого репо, см. AGENTS.md)."""
    base = Path(docs_path)
    sections: list[str] = []
    for filename in _DEFAULT_CONTEXT_FILES:
        file_path = base / filename
        if not file_path.exists():
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if filename == "AI_CHANGELOG.md":
            lines = text.splitlines()
            text = "\n".join(lines[:_CHANGELOG_TAIL_LINES])
        sections.append(f"## {filename}\n{text}")

    combined = "\n\n".join(sections)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n...(обрезано)"
    return combined
