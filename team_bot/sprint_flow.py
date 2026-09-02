"""Сценарии спринта в боте: планирование, синхронизация, итоги (TASK-SYS-1).

Отдельный модуль, а не ещё пятьсот строк в `main.py`: там уже полторы тысячи,
и следующему человеку будет проще найти всё про спринты в одном файле, чем
искать по всему боту.

## Клиентский путь, ради которого всё это

**Понедельник, начало спринта.** Бот сам закрывает прошлый спринт, рассылает
персональные итоги в личку, публикует командные в чат и открывает новый. В
чате появляется один вопрос с тремя кнопками: «Занят / Обычно / Есть время».
Одно касание — заявка о занятости готова. Команд запоминать не надо.

**Через пару часов** (или по команде `/plan`) бот показывает предложение, как
разложить бэклог по людям в пределах заявленных часов. Это предложение, а не
назначение: задачи берут сами, кнопкой под сообщением или на доске.

**Каждый день** приходит короткое личное напоминание, что на человеке. Если
кто-то двигает карточку в Miro или закрывает задачу в Напоминаниях — владелец
получает личное сообщение. Не в общий чат: там это шум, который перестают
читать на третий день.

**Закончились задачи.** Человек пишет `/more`, получает три самых приоритетных
из бэклога и берёт кнопкой. Команда узнаёт об этом сама — бот пишет в чат.
Это единственный момент, когда чужая инициатива видна без того, чтобы человек
о ней рассказывал.

**Конец спринта.** Личные итоги с цифрами, командные — в чат, новый спринт
открывается сам. Круг замыкается без единого ручного шага.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from shared import epics, sprint_planner, sprint_report
from shared.miro_client import MiroBoard
from shared.sprints import LEVEL_BUSY, LEVEL_FREE, LEVEL_NORMAL, SprintStore
from shared.sync_engine import SyncState, sync_miro, sync_reminders
from shared.task_store import TaskNotFound, TaskStore

logger = logging.getLogger(__name__)

# Как часто подтягиваем изменения из Miro и Напоминаний. Пять минут —
# компромисс: чаще упрёмся в лимиты Miro, реже человек успеет подумать, что
# доска не работает, и вернуться к старому способу «написать в чат».
SYNC_INTERVAL_SECONDS = 300


class SprintFlow:
    """Всё про спринты: команды, кнопки и фоновые циклы.

    Зависимости передаются снаружи, а не создаются внутри: `main.py` уже держит
    бота, конфиг и доску задач, и второй экземпляр той же SQLite-базы был бы
    источником трудноуловимых расхождений.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        tasks: TaskStore,
        sprints: SprintStore,
        sync_state: SyncState,
        team_chat_id: str,
        miro_token: str = "",
        miro_board_id: str = "",
        reminders_factory=None,
        llm=None,
        webapp_url: str = "",
    ) -> None:
        self.bot = bot
        self.tasks = tasks
        self.sprints = sprints
        self.sync_state = sync_state
        self.team_chat_id = team_chat_id
        self._miro_token = miro_token
        self._miro_board_id = miro_board_id
        self._reminders_factory = reminders_factory
        self.llm = llm
        self.webapp_url = webapp_url

    # ------------------------------------------------------------------
    # Внешние инструменты
    # ------------------------------------------------------------------

    def board_for(self, sprint) -> MiroBoard | None:
        """Доска спринта: своя, если задана, иначе общая из настроек."""
        board_id = (sprint.miro_board_id if sprint else "") or self._miro_board_id
        if not (self._miro_token and board_id):
            return None
        return MiroBoard(self._miro_token, board_id)

    def reminders(self):
        if self._reminders_factory is None:
            return None
        try:
            return self._reminders_factory()
        except Exception:  # noqa: BLE001
            # Напоминания — зеркало, а не условие работы доски.
            logger.exception("Не удалось подключиться к Напоминаниям")
            return None

    # ------------------------------------------------------------------
    # Синхронизация
    # ------------------------------------------------------------------

    async def run_sync(self) -> tuple[int, list[str]]:
        """Один проход синхронизации. Возвращает (сколько изменений, ошибки).

        Уведомления рассылаются здесь же: смысл синхронизации не в том, чтобы
        данные совпали, а в том, чтобы человек узнал об изменении своей задачи.
        """
        sprint = self.sprints.current()
        result = await asyncio.to_thread(self._sync_blocking, sprint)

        for change in result.changes:
            await self._notify(change)
        for error in result.errors:
            logger.warning("Синхронизация: %s", error)
        return len(result.changes), result.errors

    def _sync_blocking(self, sprint):
        """Сеть и SQLite — в отдельном потоке: обе операции блокирующие, а
        бот в это время должен продолжать отвечать людям."""
        from shared.sync_engine import SyncResult

        result = SyncResult()
        board = self.board_for(sprint)
        if board is not None:
            result.extend(
                sync_miro(self.tasks, self.sync_state, board, sprint_id=sprint.id if sprint else None)
            )
        reminders = self.reminders()
        if reminders is not None:
            result.extend(sync_reminders(self.tasks, self.sync_state, reminders))
        return result

    async def _notify(self, change) -> None:
        """Личное сообщение владельцу задачи.

        Некому писать — молчим. Слать такие изменения в общий чат нельзя: за
        день их набирается два десятка, и чат перестают читать вообще.
        """
        if not change.notify_user_id:
            return
        try:
            await self.bot.send_message(change.notify_user_id, change.as_line())
        except Exception:  # noqa: BLE001
            # Человек не начинал переписку с ботом — это нормально и не ошибка.
            logger.debug("Не смог написать пользователю %s", change.notify_user_id)

    async def sync_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(SYNC_INTERVAL_SECONDS)
                await self.run_sync()
            except Exception:
                logger.exception("Sync loop iteration failed")

    # ------------------------------------------------------------------
    # Жизненный цикл спринта
    # ------------------------------------------------------------------

    async def close_and_open(self, *, goal: str = "") -> None:
        """Закрыть текущий спринт, разослать итоги и открыть следующий."""
        current = self.sprints.current()
        if current is not None:
            await self.send_results(current)
        new_sprint = self.sprints.start(goal=goal)
        await self._announce_new_sprint(new_sprint)

    async def send_results(self, sprint) -> None:
        sprint_tasks = self.tasks.list_by_sprint(sprint.id)
        results = sprint_report.collect(sprint_tasks, sprint.capacities)
        unassigned = [t for t in sprint_tasks if not t.claimed_by and t.status != "done"]

        # Сначала личные — человек должен узнать про свои итоги раньше, чем
        # увидит общую таблицу в чате.
        for result in results:
            if not result.person_user_id:
                continue
            try:
                await self.bot.send_message(
                    result.person_user_id, sprint_report.render_personal(result, sprint)
                )
            except Exception:  # noqa: BLE001
                logger.debug("Личные итоги не доставлены: %s", result.person)

        if self.team_chat_id:
            await self.bot.send_message(
                self.team_chat_id,
                sprint_report.render_team(results, sprint, unassigned=unassigned),
            )

        # Незакрытые задачи переезжают в бэклог, а не остаются в закрытом
        # спринте: иначе они исчезнут из поля зрения вместе с ним.
        for task in sprint_tasks:
            if task.status not in ("done", "cancelled"):
                self.tasks.set_sprint(task.id, None)

    async def _announce_new_sprint(self, sprint) -> None:
        if not self.team_chat_id:
            return
        text = (
            f"🚀 <b>Новый спринт</b> — {escape(sprint.title)}\n"
            f"Две недели: {sprint.period_label}\n"
            + (f"Цель: {escape(sprint.goal)}\n" if sprint.goal else "")
            + "\nСкажите, насколько вы загружены — я предложу, как разложить задачи."
        )
        await self.bot.send_message(self.team_chat_id, text, reply_markup=_capacity_keyboard())

    async def lifecycle_loop(self) -> None:
        """Раз в час смотрим, не пора ли закрыть спринт.

        Проверка по часам, а не таймер на две недели: таймер не переживает
        перезапуск бота, а перезапускается он регулярно.
        """
        while True:
            try:
                await asyncio.sleep(3600)
                sprint = self.sprints.current()
                if sprint is None:
                    continue
                ends = datetime.fromisoformat(sprint.ends_at)
                if datetime.now(timezone.utc) >= ends:
                    await self.close_and_open()
            except Exception:
                logger.exception("Sprint lifecycle iteration failed")


# ----------------------------------------------------------------------
# Клавиатуры
# ----------------------------------------------------------------------


def _capacity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 Занят", callback_data="cap:busy"),
                InlineKeyboardButton(text="🟡 Обычно", callback_data="cap:normal"),
                InlineKeyboardButton(text="🟢 Есть время", callback_data="cap:free"),
            ]
        ]
    )


def _take_keyboard(task_ids: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Взять #{task_id}", callback_data=f"take:{task_id}")]
            for task_id in task_ids
        ]
    )


# ----------------------------------------------------------------------
# Регистрация команд
# ----------------------------------------------------------------------


def register(dp: Dispatcher, flow: SprintFlow) -> None:
    """Подключить сценарии спринта к боту."""

    @dp.message(Command("sprint"))
    async def cmd_sprint(message: Message) -> None:
        sprint = flow.sprints.current()
        if sprint is None:
            await message.answer("Активного спринта нет. Открыть: /startsprint [цель]")
            return
        tasks = flow.tasks.list_by_sprint(sprint.id)
        done = [t for t in tasks if t.status == "done"]
        lines = [
            f"🎯 <b>{escape(sprint.title)}</b>",
            f"Период: {sprint.period_label} · осталось дней: {sprint.days_left()}",
        ]
        if sprint.goal:
            lines.append(f"Цель: {escape(sprint.goal)}")
        lines.append(f"Задач: {len(tasks)}, закрыто: {len(done)}")
        if sprint.capacities:
            lines.append("")
            lines.append("Заявленная загрузка:")
            for capacity in sprint.capacities:
                lines.append(f"   {escape(capacity.person)} — {capacity.level_title}, {capacity.hours:g} ч")
        else:
            lines.append("")
            lines.append("Загрузку ещё никто не заявил — нажмите кнопку ниже.")
        await message.answer("\n".join(lines), reply_markup=_capacity_keyboard())

    @dp.message(Command("startsprint"))
    async def cmd_start_sprint(message: Message, command: CommandObject) -> None:
        goal = (command.args or "").strip()
        await flow.close_and_open(goal=goal)
        await message.answer("Спринт открыт. Итоги прошлого разослал.")

    @dp.message(Command("capacity"))
    async def cmd_capacity(message: Message, command: CommandObject) -> None:
        sprint = flow.sprints.current()
        if sprint is None:
            await message.answer("Сначала откройте спринт: /startsprint")
            return
        args = (command.args or "").strip()
        if not args:
            await message.answer("Насколько ты загружен на этот спринт?", reply_markup=_capacity_keyboard())
            return
        # Можно и числом: «/capacity 12» — для тех, кто считает часы точнее.
        hours = _parse_hours(args)
        level = _level_from_hours(hours) if hours else _level_from_word(args)
        capacity = flow.sprints.declare_capacity(
            sprint.id,
            person=_person(message),
            person_user_id=message.from_user.id if message.from_user else None,
            level=level,
            hours=hours,
        )
        await message.answer(f"Записал: {capacity.level_title}, ориентир {capacity.hours:g} ч.")

    @dp.callback_query(F.data.startswith("cap:"))
    async def on_capacity(callback: CallbackQuery) -> None:
        sprint = flow.sprints.current()
        if sprint is None:
            await callback.answer("Спринт не открыт", show_alert=True)
            return
        level = callback.data.split(":", 1)[1]
        capacity = flow.sprints.declare_capacity(
            sprint.id,
            person=_person(callback),
            person_user_id=callback.from_user.id if callback.from_user else None,
            level=level,
        )
        await callback.answer(f"Записал: {capacity.level_title}")

        # Дописываем в то же сообщение, кто уже ответил. Живой список вместо
        # десятка отдельных «записал» в чате: видно, кого ещё ждём, и никто не
        # спрашивает «все сказали?».
        declared = flow.sprints.capacities(sprint.id)
        summary = ", ".join(f"{c.person} — {c.level_title}" for c in declared)
        try:
            await callback.message.edit_text(
                f"{_strip_declared(callback.message.html_text)}\n\n"
                f"<b>Уже сказали:</b> {escape(summary)}",
                reply_markup=_capacity_keyboard(),
            )
        except Exception:  # noqa: BLE001
            # Сообщение могли удалить или оно слишком старое для правки —
            # это не повод ронять обработчик, заявка уже записана.
            logger.debug("Не смог обновить сообщение о загрузке")

    @dp.message(Command("plan"))
    async def cmd_plan(message: Message) -> None:
        sprint = flow.sprints.current()
        if sprint is None:
            await message.answer("Сначала откройте спринт: /startsprint")
            return
        backlog = flow.tasks.list_backlog()
        history = flow.tasks.list_done_since(datetime.now(timezone.utc) - timedelta(days=90))
        proposal = sprint_planner.propose(backlog, flow.sprints.capacities(sprint.id), history=history)
        await message.answer(sprint_planner.render(proposal, sprint_title=sprint.title))
        explanation = await asyncio.to_thread(sprint_planner.explain, proposal, flow.llm)
        if explanation:
            await message.answer(explanation)

    @dp.message(Command("more"))
    async def cmd_more(message: Message) -> None:
        person = _person(message)
        backlog = flow.tasks.list_backlog()
        text = sprint_report.suggest_more(backlog, person=person)
        ids = [t.id for t in sorted(backlog, key=lambda t: (t.priority, t.created_at or ""))[:3]]
        await message.answer(text, reply_markup=_take_keyboard(ids) if ids else None)

    @dp.callback_query(F.data.startswith("take:"))
    async def on_take(callback: CallbackQuery) -> None:
        task_id = int(callback.data.split(":", 1)[1])
        person = _person(callback)
        try:
            task = flow.tasks.claim_task(
                task_id, person, callback.from_user.id if callback.from_user else None
            )
        except TaskNotFound:
            await callback.answer("Задача не найдена", show_alert=True)
            return
        sprint = flow.sprints.current()
        if sprint is not None and task.sprint_id is None:
            flow.tasks.set_sprint(task.id, sprint.id)
        await callback.answer(f"Взял #{task.id}")
        if flow.team_chat_id:
            await flow.bot.send_message(
                flow.team_chat_id, sprint_report.announce_pickup(person, task)
            )

    @dp.message(Command("backlog"))
    async def cmd_backlog(message: Message) -> None:
        backlog = flow.tasks.list_backlog()
        if not backlog:
            await message.answer("Бэклог пуст.")
            return
        by_epic: dict[str, list] = {}
        for task in backlog:
            by_epic.setdefault(task.epic or epics.UNSORTED, []).append(task)
        lines = [f"📋 <b>Бэклог</b> — {len(backlog)} задач", ""]
        for code, tasks in sorted(by_epic.items(), key=lambda item: -len(item[1])):
            lines.append(f"<b>{epics.label(code)}</b> ({len(tasks)})")
            for task in sorted(tasks, key=lambda t: t.priority)[:5]:
                lines.append(f"   • #{task.id} {escape(task.title)} [P{task.priority}]")
            if len(tasks) > 5:
                lines.append(f"   … и ещё {len(tasks) - 5}")
            lines.append("")
        await message.answer("\n".join(lines))

    @dp.message(Command("epic"))
    async def cmd_epic(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Так: /epic 12 marketing\nЭпики: " + ", ".join(epics.all_codes())
            )
            return
        try:
            task = flow.tasks.set_epic(int(parts[0]), parts[1].strip().lower())
        except (ValueError, TaskNotFound):
            await message.answer("Не нашёл такую задачу.")
            return
        await message.answer(f"#{task.id} → {epics.label(task.epic)}")

    @dp.message(Command("prio"))
    async def cmd_prio(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.answer("Так: /prio 12 0  (0 — сначала, 3 — когда дойдут руки)")
            return
        try:
            task = flow.tasks.set_priority(int(parts[0]), int(parts[1]))
        except (ValueError, TaskNotFound):
            await message.answer("Не нашёл такую задачу.")
            return
        await message.answer(f"#{task.id} → приоритет P{task.priority}")

    @dp.message(Command("est"))
    async def cmd_est(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.answer("Так: /est 12 4  (примерные часы)")
            return
        try:
            task = flow.tasks.set_estimate(int(parts[0]), float(parts[1].replace(",", ".")))
        except (ValueError, TaskNotFound):
            await message.answer("Не нашёл такую задачу.")
            return
        await message.answer(f"#{task.id} → ~{task.estimate_hours:g} ч")

    @dp.message(Command("syncnow"))
    async def cmd_sync_now(message: Message) -> None:
        await message.answer("Синхронизирую…")
        count, errors = await flow.run_sync()
        if errors:
            await message.answer("Не всё получилось:\n" + "\n".join(errors[:5]))
            return
        await message.answer(f"Готово. Изменений: {count}.")

    @dp.message(Command("miro"))
    async def cmd_miro(message: Message, command: CommandObject) -> None:
        sprint = flow.sprints.current()
        board_id = (command.args or "").strip()
        if board_id and sprint is not None:
            # Принимаем и ссылку целиком: id доски — это часть после /board/.
            flow.sprints.set_board(sprint.id, _board_id_from(board_id))
            await message.answer("Доска привязана к спринту. Запускаю синхронизацию…")
            await flow.run_sync()
            return
        board = flow.board_for(sprint)
        if board is None:
            await message.answer(
                "Доска Miro не настроена. Пришлите ссылку: /miro https://miro.com/app/board/XXXX/"
            )
            return
        await message.answer("Доска подключена. /syncnow — синхронизировать вручную.")


# ----------------------------------------------------------------------
# Мелочи
# ----------------------------------------------------------------------


def _strip_declared(text: str) -> str:
    """Убирает прошлый список ответивших, чтобы он не копился при каждом нажатии."""
    marker = "\n\nУже сказали:"
    index = text.find(marker)
    return text[:index] if index > 0 else text


def _person(source) -> str:
    user = getattr(source, "from_user", None)
    if user is None:
        return "кто-то"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def _parse_hours(text: str) -> float | None:
    try:
        value = float(text.replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None
    return value if 0 < value <= 200 else None


def _level_from_hours(hours: float) -> str:
    if hours <= 12:
        return LEVEL_BUSY
    if hours <= 24:
        return LEVEL_NORMAL
    return LEVEL_FREE


def _level_from_word(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("занят", "busy", "загруж", "мало")):
        return LEVEL_BUSY
    if any(word in lowered for word in ("свобод", "free", "много", "есть время")):
        return LEVEL_FREE
    return LEVEL_NORMAL


def _board_id_from(value: str) -> str:
    """id доски из ссылки вида https://miro.com/app/board/uXjVK.../ .

    Принимаем и голый id: человек скорее пришлёт ссылку, но заставлять его
    вырезать из неё кусок — лишний повод ошибиться.
    """
    value = value.strip().rstrip("/")
    if "miro.com" not in value:
        return value
    parts = [part for part in value.split("/") if part]
    for index, part in enumerate(parts):
        if part == "board" and index + 1 < len(parts):
            return parts[index + 1].split("?")[0]
    return value
