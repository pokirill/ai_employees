"""Итоги спринта: командные и персональные (TASK-SYS-1).

## Почему персональные итоги отдельно от командной сводки

Командная сводка (`shared/sprint_digest.py`) отвечает на вопрос «что мы сделали»
и уходит в общий чат. Персональная отвечает на другой вопрос — «как прошло у
меня» — и уходит в личку. Смешивать нельзя: разбор чужой невыполненной задачи в
общем чате читается как публичный выговор, даже если формулировка мягкая.

## Тон

Хвалим за факт, а не авансом. «Закрыл 6 из 6» — это факт. «Ты молодец» без
цифры рядом обесценивается на третий спринт, и человек перестаёт читать сводку.

Про незакрытые задачи говорим спокойно и без выводов о человеке. Причин не
закрыть задачу десятки, и почти все — не про лень. Сводка не место, где
выясняют, почему; она место, где показывают, что было.

## Без модели

Весь текст собирается из чисел. Модель здесь не нужна и вредна: раз в две
недели каждому человеку в личку — это ровно тот случай, когда неудачная
формулировка обходится дороже, чем польза от красивого языка.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from shared import epics
from shared.sprints import Capacity, Sprint
from shared.task_store import Task


@dataclass
class PersonResult:
    person: str
    person_user_id: int | None
    done: list[Task]
    open_left: list[Task]
    capacity: Capacity | None

    @property
    def planned(self) -> int:
        return len(self.done) + len(self.open_left)

    @property
    def completion_rate(self) -> int | None:
        if not self.planned:
            return None
        return round(len(self.done) / self.planned * 100)


def collect(sprint_tasks: list[Task], capacities: list[Capacity]) -> list[PersonResult]:
    """Разложить задачи спринта по людям.

    Берём только те, у которых есть исполнитель: задача, которую никто не взял,
    — это результат команды, а не человека, и в персональную сводку ей нельзя.
    """
    by_person: dict[str, PersonResult] = {}
    capacity_by_person = {c.person: c for c in capacities}

    for task in sprint_tasks:
        person = task.claimed_by
        if not person:
            continue
        if person not in by_person:
            by_person[person] = PersonResult(
                person=person,
                person_user_id=task.claimed_by_user_id,
                done=[],
                open_left=[],
                capacity=capacity_by_person.get(person),
            )
        entry = by_person[person]
        if task.claimed_by_user_id and not entry.person_user_id:
            entry.person_user_id = task.claimed_by_user_id
        if task.status == "done":
            entry.done.append(task)
        elif task.status != "cancelled":
            entry.open_left.append(task)

    return sorted(by_person.values(), key=lambda r: r.person)


def render_personal(result: PersonResult, sprint: Sprint) -> str:
    """Личная сводка (HTML parse_mode). Одна на человека, в личку."""
    lines = [f"🏁 <b>Спринт закрыт</b> — {escape(sprint.title)}", ""]

    if not result.planned:
        return (
            f"🏁 <b>Спринт закрыт</b> — {escape(sprint.title)}\n\n"
            "За этот спринт на тебе не было задач. Если это не так и задачи "
            "были, значит они не отмечены как твои на доске — стоит поправить, "
            "иначе следующая сводка снова будет пустой."
        )

    rate = result.completion_rate
    lines.append(f"Закрыто <b>{len(result.done)} из {result.planned}</b> ({rate}%)")

    if result.done:
        lines.append("")
        lines.append("✅ <b>Сделано:</b>")
        for task in result.done:
            lines.append(f"   • {escape(task.title)} {epics.get(task.epic).emoji}")

    if result.open_left:
        lines.append("")
        lines.append("➡️ <b>Переносится:</b>")
        for task in result.open_left:
            lines.append(f"   • {escape(task.title)}")

    lines.append("")
    lines.append(_verdict(result))
    return "\n".join(lines)


def _plural(count: int, one: str, few: str, many: str) -> str:
    """«1 задача», «2 задачи», «5 задач».

    Мелочь, но сводка приходит каждому человеку раз в две недели, и «1 задач»
    в ней читается как небрежность — а вместе с ней обесценивается и остальное.
    """
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _verdict(result: PersonResult) -> str:
    """Одна фраза в конце. Только на основании чисел, без домыслов."""
    rate = result.completion_rate or 0
    done = len(result.done)
    left = len(result.open_left)

    if left == 0 and done > 0:
        return f"Весь план закрыт — {done} из {done}. Это редко у кого получается два спринта подряд."
    if rate >= 80:
        tail = _plural(left, "задача", "задачи", "задач")
        return f"Почти всё: {done} из {result.planned}. Осталась {left} {tail} — перенёс в следующий спринт."
    if rate >= 50:
        tail = _plural(left, "задача", "задачи", "задач")
        verb = "переезжает" if left % 10 == 1 and left % 100 != 11 else "переезжают"
        return (
            f"Больше половины плана закрыто. {left} {tail} {verb} — "
            "посмотри, не пора ли их разбить помельче."
        )
    if done == 0:
        return (
            "Ни одна задача не закрыта. Если они оказались крупнее, чем выглядели, "
            "имеет смысл разбить их на части — так виден прогресс, а не только финиш."
        )
    return (
        f"Закрыто {done} из {result.planned}. Похоже, спринт был набран плотнее, чем получилось вытянуть — "
        "на следующем можно заявить меньше часов, это нормально."
    )


def render_team(results: list[PersonResult], sprint: Sprint, *, unassigned: list[Task]) -> str:
    """Командная часть итогов — в общий чат, без разбора персональных провалов."""
    total_done = sum(len(r.done) for r in results)
    total_planned = sum(r.planned for r in results)

    lines = [
        f"🏁 <b>Итоги спринта</b> — {escape(sprint.title)}",
        f"Период: {sprint.period_label}",
    ]
    if sprint.goal:
        lines.append(f"Цель: {escape(sprint.goal)}")
    lines.append("")

    if total_planned:
        lines.append(f"Закрыто <b>{total_done} из {total_planned}</b> взятых задач")
    else:
        lines.append("Взятых задач в этом спринте не было")

    if results:
        lines.append("")
        for result in results:
            rate = result.completion_rate
            lines.append(
                f"   {escape(result.person)}: {len(result.done)}/{result.planned}"
                + (f" ({rate}%)" if rate is not None else "")
            )

    if unassigned:
        lines.append("")
        lines.append(
            f"🫥 Задач без исполнителя: {len(unassigned)}. "
            "Это не чья-то вина, а знак, что их никто не взял — стоит либо разобрать, либо убрать из спринта."
        )

    lines.append("")
    lines.append(_by_epic_line(results))
    return "\n".join(lines)


def _by_epic_line(results: list[PersonResult]) -> str:
    """Куда ушла работа. Полезнее общего счётчика: показывает перекос."""
    counts: dict[str, int] = {}
    for result in results:
        for task in result.done:
            code = task.epic or epics.UNSORTED
            counts[code] = counts.get(code, 0) + 1
    if not counts:
        return "По эпикам: пока нечего разложить."
    parts = [
        f"{epics.get(code).emoji} {epics.get(code).title} — {count}"
        for code, count in sorted(counts.items(), key=lambda item: -item[1])
    ]
    return "Куда ушла работа: " + ", ".join(parts)


def suggest_more(backlog: list[Task], *, person: str, limit: int = 3) -> str:
    """Что предложить человеку, который просит ещё задач.

    Берём самые приоритетные из бэклога. Не назначаем — показываем, чтобы он
    сам взял: инициатива, у которой отняли выбор, перестаёт быть инициативой.
    """
    if not backlog:
        return (
            "Свободных задач в бэклоге нет — это хорошая новость. "
            "Если есть идея, что стоит сделать, заведи её через /task."
        )
    ordered = sorted(backlog, key=lambda t: (t.priority, t.created_at or ""))[:limit]
    lines = [f"Вот что можно взять, {escape(person)}:", ""]
    for task in ordered:
        epic = epics.get(task.epic)
        estimate = f", ~{task.estimate_hours:g} ч" if task.estimate_hours else ""
        lines.append(f"   • #{task.id} {escape(task.title)} [{epic.emoji} P{task.priority}{estimate}]")
    lines.append("")
    lines.append("Берётся командой /claim &lt;номер&gt;. Команде я скажу сам.")
    return "\n".join(lines)


def announce_pickup(person: str, task: Task) -> str:
    """Сообщение в общий чат, когда человек добрал задачу сверх плана.

    Зачем в общий чат: это единственный момент, когда чужая инициатива видна
    без того, чтобы человек сам о ней рассказывал. Молчать здесь — значит
    сделать вид, что ничего не произошло.
    """
    return (
        f"💪 {escape(person)} закрыл свой план и взял сверху: "
        f"#{task.id} «{escape(task.title)}» {epics.get(task.epic).emoji}"
    )
