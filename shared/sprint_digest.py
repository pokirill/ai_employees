from __future__ import annotations

from html import escape

from shared.task_store import Task


def build_sprint_digest(done: list[Task], cancelled: list[Task], still_open: list[Task], *, period_label: str) -> str | None:
    """Недельный итог спринта для чата команды (HTML parse_mode) — СУПЕР
    короткий, без единого вызова LLM (см. R-COST в team_bot/main.py:
    sprint_loop). Собран целиком из структурных данных доски: что сделали,
    что отменили, что осталось открытым (= перенеслось на новый спринт).
    None, если за весь период вообще не было активности — нечего подводить."""
    if not done and not cancelled and not still_open:
        return None

    lines = [
        f"📊 <b>Итоги спринта</b> ({period_label})",
        _bucket_line("✅ Сделали", done),
        _bucket_line("❌ Отменили", cancelled),
        _bucket_line("➡️ Перенесли", still_open),
        _success_rate_line(done, cancelled),
    ]
    return "\n".join(lines)


def _success_rate_line(done: list[Task], cancelled: list[Task]) -> str:
    # По просьбе Кирилла: оценки должны быть количественными, не только
    # качественными — это единственная цифра, которую можно посчитать
    # честно из структурных данных доски (без LLM): из задач, доведённых до
    # какого-то исхода (сделано ИЛИ отменено) за период, сколько реально
    # сделано. "Перенесли" сюда не входит — они ещё не дошли до исхода.
    closed = len(done) + len(cancelled)
    if not closed:
        return "📈 Доля выполненных из закрытых: — (пока нет закрытых задач)"
    rate = round(len(done) / closed * 100)
    return f"📈 Доля выполненных из закрытых: {rate}% ({len(done)}/{closed})"


def _bucket_line(label: str, tasks: list[Task]) -> str:
    if not tasks:
        return f"{label}: —"
    titles = ", ".join(f"«{escape(t.title)}»{_status_suffix(t)}" for t in tasks)
    return f"{label} ({len(tasks)}): {titles}"


def _status_suffix(task: Task) -> str:
    return " [тестируется]" if task.status == "testing" else ""
