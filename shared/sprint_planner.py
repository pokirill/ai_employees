"""Предложение состава спринта и распределения задач (TASK-SYS-1).

## Главное правило: система предлагает, назначает человек

Ни одна функция здесь не записывает исполнителя. Она возвращает предложение,
которое команда видит в чате и принимает или меняет. Автоматически назначенная
задача — это задача, о которой человек узнал постфактум; после пары таких
случаев в инструмент перестают верить, и он превращается в ещё одно место, куда
никто не смотрит.

## Как считается предложение

1. Берём бэклог, сортируем по приоритету, потом по возрасту. Старая задача
   с тем же приоритетом идёт раньше: иначе она не уедет никогда.
2. Раскладываем по людям, пока не упрёмся в заявленные часы. Оценки нет —
   считаем задачу за `DEFAULT_ESTIMATE_HOURS`: без какого-то числа
   распределение выродится в «всё одному».
3. Учитываем, кто чем занимался раньше: у кого больше закрытых задач эпика,
   тому эпик и предлагаем. Это не жёсткое правило, а предпочтение — иначе один
   человек навсегда останется единственным, кто трогает бэкенд.

Расчёт арифметический, без модели. Модель нужна только чтобы объяснить
предложение словами — и её отсутствие ничего не ломает.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared import epics
from shared.sprints import Capacity
from shared.task_store import Task

# Задача без оценки. Полдня — намеренно много: лучше недоложить в спринт, чем
# набрать двадцать «мелких» задач, каждая из которых оказалась на день.
DEFAULT_ESTIMATE_HOURS = 4.0

# Насколько сильно опыт в эпике влияет на выбор исполнителя. Небольшой бонус:
# предпочтение, а не закрепление человека за областью навсегда.
EXPERIENCE_BONUS = 0.15


@dataclass
class PersonPlan:
    person: str
    person_user_id: int | None
    capacity_hours: float
    tasks: list[Task] = field(default_factory=list)

    @property
    def planned_hours(self) -> float:
        return sum(t.estimate_hours or DEFAULT_ESTIMATE_HOURS for t in self.tasks)

    @property
    def free_hours(self) -> float:
        return max(0.0, self.capacity_hours - self.planned_hours)


@dataclass
class Proposal:
    plans: list[PersonPlan]
    # Что не поместилось ни к кому. Показывать обязательно: молча не влезшая
    # задача — это задача, о которой все забыли.
    left_out: list[Task] = field(default_factory=list)

    @property
    def planned_count(self) -> int:
        return sum(len(plan.tasks) for plan in self.plans)


def propose(
    backlog: list[Task],
    capacities: list[Capacity],
    *,
    history: list[Task] | None = None,
) -> Proposal:
    """Разложить бэклог по людям в пределах заявленных часов.

    `history` — закрытые задачи прошлых спринтов, по ним считается опыт в
    эпиках. Пусто — опыт просто не учитывается.
    """
    if not capacities:
        return Proposal(plans=[], left_out=list(backlog))

    experience = _experience_by_person(history or [])
    plans = [
        PersonPlan(person=c.person, person_user_id=c.person_user_id, capacity_hours=c.hours)
        for c in capacities
    ]

    ordered = sorted(
        backlog,
        key=lambda t: (t.priority if t.priority is not None else 2, t.created_at or ""),
    )

    left_out: list[Task] = []
    for task in ordered:
        cost = task.estimate_hours or DEFAULT_ESTIMATE_HOURS
        candidate = _best_candidate(plans, task, cost, experience)
        if candidate is None:
            left_out.append(task)
            continue
        candidate.tasks.append(task)

    return Proposal(plans=plans, left_out=left_out)


def _best_candidate(
    plans: list[PersonPlan], task: Task, cost: float, experience: dict[str, dict[str, int]]
) -> PersonPlan | None:
    """Кому предложить задачу.

    Берём того, у кого больше всего свободных часов, с поправкой на опыт в
    эпике. Если ни у кого не хватает — None: раздувать чью-то загрузку сверх
    заявленной нельзя, человек ведь именно об этом и предупредил.
    """
    fitting = [plan for plan in plans if plan.free_hours >= cost]
    if not fitting:
        return None

    def score(plan: PersonPlan) -> float:
        base = plan.free_hours
        if task.epic:
            done_in_epic = experience.get(plan.person, {}).get(task.epic, 0)
            base += min(done_in_epic, 5) * EXPERIENCE_BONUS * plan.capacity_hours / 10
        return base

    return max(fitting, key=score)


def _experience_by_person(history: list[Task]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for task in history:
        if not task.claimed_by or not task.epic:
            continue
        result.setdefault(task.claimed_by, {})
        result[task.claimed_by][task.epic] = result[task.claimed_by].get(task.epic, 0) + 1
    return result


def render(proposal: Proposal, *, sprint_title: str) -> str:
    """Предложение текстом для чата (HTML parse_mode).

    Формулировки намеренно в сослагательном: это предложение, а не назначение.
    Человек должен видеть, что решение всё ещё за ним.
    """
    from html import escape

    if not proposal.plans:
        return (
            "Никто ещё не сказал, насколько занят на этот спринт. "
            "Напишите боту /capacity — и я предложу, как разложить задачи."
        )

    lines = [f"🗂 <b>Предложение по спринту</b> — {escape(sprint_title)}", ""]
    for plan in sorted(proposal.plans, key=lambda p: p.person):
        header = (
            f"<b>{escape(plan.person)}</b> — "
            f"{_hours(plan.planned_hours)} из {_hours(plan.capacity_hours)}"
        )
        lines.append(header)
        if not plan.tasks:
            lines.append("   — ничего не подобралось")
        for task in plan.tasks:
            lines.append(
                f"   • #{task.id} {escape(task.title)} "
                f"[{epics.get(task.epic).emoji} P{task.priority}]"
            )
        lines.append("")

    if proposal.left_out:
        lines.append(f"⚠️ <b>Не поместилось ({len(proposal.left_out)})</b> — останется в бэклоге:")
        for task in proposal.left_out[:10]:
            lines.append(f"   • #{task.id} {escape(task.title)} [P{task.priority}]")
        if len(proposal.left_out) > 10:
            lines.append(f"   … и ещё {len(proposal.left_out) - 10}")
        lines.append("")

    lines.append("Это предложение, а не назначение: берите через /claim или на доске.")
    return "\n".join(lines)


def _hours(value: float) -> str:
    return f"{value:g} ч"


def explain(proposal: Proposal, llm) -> str:
    """Короткое объяснение предложения словами. Без модели — пустая строка.

    Отдельно от `render`, потому что текст предложения обязан появляться
    всегда, а объяснение — приятное дополнение, ради которого нельзя рисковать
    тем, что команда вообще не увидит план.
    """
    if llm is None:
        return ""
    summary = "; ".join(
        f"{plan.person}: {len(plan.tasks)} задач на {plan.planned_hours:g} ч из {plan.capacity_hours:g}"
        for plan in proposal.plans
    )
    prompt = (
        "Ты тимлид. Объясни команде в двух-трёх предложениях, почему спринт "
        "разложен так, и на что обратить внимание. Без воды и без похвалы. "
        f"Данные: {summary}. Не поместилось задач: {len(proposal.left_out)}."
    )
    try:
        return (llm.chat([{"role": "user", "content": prompt}], max_tokens=200, temperature=0.3) or "").strip()
    except Exception:
        return ""
