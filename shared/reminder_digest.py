from __future__ import annotations

from html import escape

from shared.task_store import Task


def build_reminder_digest(tasks: list[Task]) -> str | None:
    """Ежедневный дайджест открытых задач для чата команды (HTML parse_mode).
    None, если открытых задач нет вообще — не шлём "всё сделано" ради самого
    себя, дайджест нужен только когда есть о чём напомнить."""
    pending = [t for t in tasks if t.status != "done"]
    if not pending:
        return None

    by_claimer: dict[tuple[str, int | None], list[Task]] = {}
    unclaimed: list[Task] = []
    for task in pending:
        if task.claimed_by:
            by_claimer.setdefault((task.claimed_by, task.claimed_by_user_id), []).append(task)
        else:
            unclaimed.append(task)

    lines = ["⏰ <b>Напоминание по доске задач</b>"]
    for (name, user_id), claimer_tasks in by_claimer.items():
        # tg://user?id= даёт настоящее кликабельное упоминание с уведомлением
        # человеку — просто имени недостаточно, Telegram не подсветит и не
        # уведомит по displayed-имени.
        mention = f'<a href="tg://user?id={user_id}">{escape(name)}</a>' if user_id else f"<b>{escape(name)}</b>"
        lines.append(f"\n{mention}:")
        lines.extend(f"• «{escape(t.title)}»{_status_suffix(t)}" for t in claimer_tasks)

    if unclaimed:
        lines.append("\nПока никто не взял:")
        lines.extend(f"• «{escape(t.title)}»" for t in unclaimed)

    return "\n".join(lines)


def _status_suffix(task: Task) -> str:
    return " (тестирование)" if task.status == "testing" else ""
