from __future__ import annotations

from html import escape

from shared.task_store import Task

# Лимит одного сообщения Telegram — 4096 символов. Держимся заметно ниже:
# у сообщения есть разметка, а обрезанный на полуслове дайджест выглядит как
# поломка.
_MAX_LENGTH = 3400

# Сколько задач показываем на человека. Больше пяти строк в напоминании никто
# не читает: список превращается в стену, и её пролистывают целиком.
_PER_PERSON = 5

# Сколько ничьих задач показываем. Их обычно много, и смысл строки — не
# перечислить всё, а напомнить, что есть свободные.
_UNCLAIMED_SHOWN = 3


def build_reminder_digest(tasks: list[Task], *, backlog_count: int = 0) -> str | None:
    """Ежедневный дайджест открытых задач для чата команды (HTML parse_mode).

    None, если открытых задач нет вообще — не шлём «всё сделано» ради самого
    себя, дайджест нужен только когда есть о чём напомнить.

    🚨 **Дайджест обязан помещаться в одно сообщение.** На 02.09.2026 на доске
    было 98 открытых задач, и дайджест выходил на 10 051 символ при лимите
    Telegram в 4096 — то есть не отправлялся вообще, и команда просто не
    получала напоминаний. Ошибка при этом уходила в лог и никому не попадалась
    на глаза.

    Поэтому здесь два ограничения: по числу задач на человека и жёсткая
    проверка длины в конце. Дайджест — это напоминание, а не выгрузка доски;
    полный список всегда есть в мини-аппе и по /tasks.

    `backlog_count` — сколько задач лежит в бэклоге. Нужен, чтобы у тех, кто
    закрыл свой план, был очевидный следующий шаг: без этой строки человек либо
    сам догадается написать /more, либо останется без задач до конца спринта.
    """
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

    lines = [f"⏰ <b>Напоминание по доске</b> · открыто {len(pending)}"]

    for (name, user_id), claimer_tasks in by_claimer.items():
        # tg://user?id= даёт настоящее кликабельное упоминание с уведомлением
        # человеку — просто имени недостаточно, Telegram не подсветит и не
        # уведомит по displayed-имени.
        mention = (
            f'<a href="tg://user?id={user_id}">{escape(name)}</a>'
            if user_id
            else f"<b>{escape(name)}</b>"
        )
        lines.append(f"\n{mention} — {len(claimer_tasks)}:")
        # Сначала то, что важнее: приоритет, потом старые.
        ordered = sorted(claimer_tasks, key=lambda t: (t.priority, t.created_at or ""))
        lines.extend(f"• «{escape(t.title)}»{_status_suffix(t)}" for t in ordered[:_PER_PERSON])
        if len(ordered) > _PER_PERSON:
            lines.append(f"• …и ещё {len(ordered) - _PER_PERSON}")

    if unclaimed:
        lines.append(f"\nНикто не взял — {len(unclaimed)}:")
        ordered = sorted(unclaimed, key=lambda t: (t.priority, t.created_at or ""))
        lines.extend(f"• «{escape(t.title)}»" for t in ordered[:_UNCLAIMED_SHOWN])
        if len(ordered) > _UNCLAIMED_SHOWN:
            lines.append(f"• …и ещё {len(ordered) - _UNCLAIMED_SHOWN} — весь список в /backlog")

    if backlog_count:
        lines.append(
            f"\nЗакрыл своё? В бэклоге {backlog_count} — напиши /more, предложу три самых важных."
        )

    return _fit(lines)


def _fit(lines: list[str]) -> str:
    """Собирает сообщение и гарантированно укладывает его в лимит.

    Страховка на случай, если задач у одного человека окажется много, а
    названия — длинными. Лучше сказать «показал не всё», чем не отправить
    ничего: именно так дайджест и пропадал.
    """
    text = "\n".join(lines)
    if len(text) <= _MAX_LENGTH:
        return text

    kept: list[str] = []
    length = 0
    tail = "\n\n…список не поместился целиком — открой доску или /tasks"
    for line in lines:
        if length + len(line) + 1 + len(tail) > _MAX_LENGTH:
            break
        kept.append(line)
        length += len(line) + 1
    return "\n".join(kept) + tail


def _status_suffix(task: Task) -> str:
    return " (тестирование)" if task.status == "testing" else ""
