from __future__ import annotations

import subprocess
from pathlib import Path

# Кандидаты на контекст — не весь Docs/, там есть многотысячестрочные
# исторические логи. FinAssist и Finik-backend называют файлы по-разному
# (FinAssist: RELEASE_NOTES.md; Finik-backend: API_CONTRACT.md/ARCHITECTURE.md),
# поэтому список общий — берём то, что реально существует в каждом репо.
_CANDIDATE_CONTEXT_FILES = [
    "BACKLOG.md",
    "AI_CHANGELOG.md",
    "RELEASE_NOTES.md",
    "API_CONTRACT.md",
    "ARCHITECTURE.md",
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


def sync_docs_repos(docs_paths: list[str]) -> None:
    for path in docs_paths:
        sync_docs_repo(path)


def load_project_context(docs_paths: list[str] | str, max_chars: int = 12_000) -> str:
    """Собирает контекст из Docs/*.md одного или нескольких репозиториев
    (FinAssist, Finik-backend) для промпта LLM. Секции подписаны именем
    репозитория, чтобы ассистент понимал, откуда какой факт.

    Бюджет max_chars делится ПОРОВНУ между всеми найденными файлами, а не
    расходуется последовательно — иначе один огромный файл (в FinAssist
    BACKLOG.md весит ~500 КБ) съедает весь бюджет целиком, и ассистент
    никогда не увидит ни AI_CHANGELOG.md, ни второй репозиторий вообще.
    """
    if isinstance(docs_paths, str):
        docs_paths = [docs_paths]

    raw_entries: list[tuple[str, str, str]] = []  # (repo_label, filename, text)
    for docs_path in docs_paths:
        base = Path(docs_path)
        repo_label = base.parent.name or str(base)
        for filename in _CANDIDATE_CONTEXT_FILES:
            file_path = base / filename
            if not file_path.exists():
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            if filename == "AI_CHANGELOG.md":
                lines = text.splitlines()
                text = "\n".join(lines[:_CHANGELOG_TAIL_LINES])
            raw_entries.append((repo_label, filename, text))

    if not raw_entries:
        return ""

    per_file_budget = max(max_chars // len(raw_entries), 500)
    sections: list[str] = []
    for repo_label, filename, text in raw_entries:
        if len(text) > per_file_budget:
            text = text[:per_file_budget] + "\n...(обрезано)"
        sections.append(f"## [{repo_label}] {filename}\n{text}")

    combined = "\n\n".join(sections)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n...(обрезано)"
    return combined
