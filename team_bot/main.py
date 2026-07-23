from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from shared.chat_log import DEFAULT_LOG_PATH, append_chat_message, format_for_prompt, messages_since
from shared.config import LLMConfig, TaskBoardConfig, TeamBotConfig
from shared.context_heuristic import question_needs_project_context
from shared.docs_context import load_project_context, sync_docs_repos, topic_context_files
from shared.icloud_reminders import ICloudReminders
from shared.icloud_reminders import TaskNotFound as ReminderTaskNotFound
from shared.llm_client import LLMClient
from shared.rate_limiter import SlidingWindowLimiter
from shared.reminder_digest import build_reminder_digest
from shared.role_agents import RoleAgent, list_role_agents, role_agent_for_command
from shared.sprint_digest import build_sprint_digest
from shared.sprint_state import current_sprint_period, save_last_sprint_at
from shared.task_store import TaskNotFound, TaskStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("team_bot")

config = TeamBotConfig()
board_config = TaskBoardConfig()
llm = LLMClient(LLMConfig(), model_override=config.model_override)
reminders = ICloudReminders(
    apple_id=config.icloud_apple_id,
    app_specific_password=config.icloud_app_password,
    list_name=config.icloud_reminders_list_name,
)
# Доска задач (мини-апп) — источник правды для claim/статус/комментариев,
# Напоминания остаются best-effort зеркалом (см. cmd_task/cmd_done).
tasks_store = TaskStore(board_config.db_path)

# R-COST: см. shared/rate_limiter.py — не более N вопросов ассистенту в час
# на чат, чтобы один болтливый чат не сжёг весь бюджет LLM.
_rate_limiter = SlidingWindowLimiter(max_calls=config.max_questions_per_hour, window_seconds=3600)

_CHAT_LOG_PATH = DEFAULT_LOG_PATH

# R-COST: контекст проекта (Docs/*.md обоих репо) — не на каждый вопрос, а
# только когда похоже, что он реально нужен (см. question_needs_project_context).
# Меньше max_chars, чем полный дефолт docs_context — команд-бот не обязан
# видеть ВСЁ, только достаточно для конкретного вопроса. Поднят с 6000 до
# 9000: с добавлением тематических файлов (topic_context_files) есть что
# реально положить в бюджет — раньше бот мог отвечать только по BACKLOG/
# CHANGELOG/ARCHITECTURE, теперь по вопросу подключаются файлы вроде
# BUSINESS_LOGIC.md/ONBOARDING_FLOW.md/GOALS_SCREEN.md и т.п. Поднят с 9000
# до 12000 при добавлении плейбука Авито как третьего корня контекста — тот
# же класс бюджетного голодания, что уже решался пропорциональным трим'ом
# (см. shared/docs_context.py), но с ещё одним репо базовый набор файлов
# растёт с 6 до 9, и без запаса бюджет на файл падал бы ниже пола в 500
# симв. (проверено реальным вызовом load_project_context с 13 секциями).
_CONTEXT_MAX_CHARS = 12_000
# R-COST: LLMConfig.reasoning_effort="minimal" (см. shared/llm_client.py)
# убирает налог на скрытые reasoning-токены reasoning-моделей — без него
# 400 не хватало (модель гасила весь бюджет на "раздумья" и возвращала
# пустую строку), с "minimal" даже меньшего бюджета хватает с запасом на
# полноценный ответ (проверено реальным вызовом API).
_ANSWER_MAX_TOKENS = 500

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message.middleware()
async def _log_team_chat_message(handler, event, data):
    # Пассивное логирование чата команды — НОЛЬ вызовов LLM здесь, просто
    # копим буфер для недельного анализа эффективности (см. sprint_loop /
    # По просьбе Кирилла: "пусть наш чат читает и использует в качестве доп
    # инфы"). Сделано как middleware, а не обычный @dp.message(...) хендлер —
    # иначе по правилу "первый подошедший хендлер съедает апдейт" это
    # выключило бы @упоминания/реплаи боту, которые должны доходить до
    # handle_assistant_message ниже.
    if (
        config.team_chat_id
        and str(event.chat.id) == config.team_chat_id
        and event.text
        and not event.text.startswith("/")
        and not (event.from_user and event.from_user.is_bot)
    ):
        author = event.from_user.full_name if event.from_user else "неизвестно"
        append_chat_message(_CHAT_LOG_PATH, author=author, text=event.text)
    return await handler(event, data)

_SYSTEM_PROMPT = (
    "Ты — ассистент команды разработки приложения «Кубышка» (iOS-репозиторий "
    "FinAssist + бэкенд Finik-backend). Отвечай кратко и по делу, на русском. "
    "Если вопрос касается архитектуры, бэклога или истории решений проекта — "
    "опирайся на контекст ниже (там пометки [FinAssist]/[Finik-backend], "
    "откуда какой факт). Если контекста не хватает — честно скажи, что не "
    "уверен, не выдумывай детали.\n"
    "В контексте также может быть раздел [avito_playbook] — это инженерный "
    "плейбук Авито (процессы, код-ревью, TDR, грейды/роли разработки). Когда "
    "вопрос касается процессов разработки, код-ревью, роли тимлида, QA, "
    "дизайна, продукта или профессиональных грейдов — ориентируйся на "
    "стандарты из этого раздела как на эталон, а не только на общие "
    "рассуждения. Если плейбук не подключён к конкретному вопросу (не в "
    "контексте) — не притворяйся, что он есть, отвечай по общим знаниям.\n"
    "Пиши только по-русски: не используй английские, испанские или другие "
    "иноязычные слова и аббревиатуры (например, недопустимо 'BBDD' вместо "
    "'БД') — используй русские термины, принятые в команде (БД, бэкенд, "
    "API можно оставить как есть — это устоявшиеся технические термины, а "
    "не случайные иноязычные вставки)."
)

# Короткая память диалога — только для реплаев-продолжений /ask, не
# персистится (перезапуск бота = чистый лист). 3 последних обмена достаточно
# для уточняющих вопросов, не разрастаясь в полноценную БД.
#
# Ключ — (chat_id, user_id), НЕ просто chat_id: в групповом чате несколько
# человек могут спрашивать ассистента одновременно, и если бы история была
# общей на чат, продолжение (reply/упоминание) одного человека подмешивало
# бы контекст чужого недавнего вопроса. Лимит вопросов в час (_rate_limiter)
# сознательно остаётся per-chat — это защита бюджета, а не память диалога,
# смешивать их не нужно.
_MAX_HISTORY_MESSAGES = 6
_HistoryKey = tuple[int, int]
_conversation_history: dict[_HistoryKey, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=_MAX_HISTORY_MESSAGES)
)


def _history_key(message: Message) -> _HistoryKey:
    user_id = message.from_user.id if message.from_user else 0
    return (message.chat.id, user_id)

# Заполняется в main() через bot.get_me() перед стартом polling — нужен, чтобы
# распознавать «@ИмяБота вопрос» в групповом чате как обращение к ассистенту.
_bot_username: str | None = None


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Привет! Я помощник команды по проекту «Кубышка».\n\n"
        "<b>Задачи</b>\n"
        "/task &lt;текст&gt; — записать задачу на общую доску\n"
        "(можно ответить командой /task на чьё-то сообщение — возьму текст оттуда)\n"
        "/tasks — показать незавершённые задачи\n"
        "/mytasks — задачи, которые взял именно я\n"
        "/claim &lt;номер&gt; — взять задачу себе\n"
        "/unclaim &lt;номер&gt; — отпустить задачу\n"
        "/testing &lt;номер&gt; — отправить на тестирование\n"
        "/done &lt;номер&gt; — отметить задачу выполненной\n"
        "/cancel &lt;номер&gt; — отметить задачу отменённой (не «сделали»)\n"
        "/comment &lt;номер&gt; &lt;текст&gt; — комментарий к задаче\n"
        "/rename &lt;номер&gt; &lt;текст&gt; — переименовать задачу\n"
        "/board — открыть доску задач (мини-апп: карточка, комментарии, статус) — в личке\n"
        "/remindnow — прислать дайджест открытых задач сейчас (обычно раз в день сам)\n"
        f"/sprintnow — превью итогов спринта за текущий период (сам — по субботам в {config.sprint_hour}:00)\n\n"
        "<b>Ассистент</b>\n"
        "/ask &lt;вопрос&gt; — спросить про проект (контекст из Docs/ обоих репо + плейбук Авито)\n"
        "В группе: упомяни меня (@бот вопрос) или ответь на моё сообщение\n"
        "В личке: пиши что угодно, отвечу как ассистент без команд\n\n"
        f"(до {config.max_questions_per_hour} вопросов ассистенту в час на чат — бережём бюджет)\n\n"
        "<b>Роли</b> (советуют от лица роли — не выполняют работу сами, см. /roles)\n"
        "/dev, /techlead, /qa, /design, /product &lt;вопрос&gt;\n\n"
        "<b>Утилита</b>\n"
        "/id — показать ID этого чата (нужно для настройки)"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject) -> None:
    title = (command.args or "").strip()
    if not title and message.reply_to_message:
        # .text для обычных сообщений, .caption — если отвечают на фото/видео
        # с подписью (например, скриншот бага с описанием в подписи).
        reply = message.reply_to_message
        title = (reply.text or reply.caption or "").strip()
    if not title:
        await message.answer(
            "Формат: /task купить домен для канала\n"
            "Или ответь командой /task на сообщение с текстом задачи."
        )
        return
    author = message.from_user.full_name if message.from_user else "неизвестно"
    task = tasks_store.add_task(title, created_by=author)
    try:
        uid = reminders.add_task(title, notes=f"От {author} в Telegram, доска #{task.id}")
        tasks_store.set_reminder_uid(task.id, uid)
    except Exception:
        # Доска — источник правды, Напоминания — best-effort зеркало для тех,
        # кто смотрит список на телефоне. Сбой зеркалирования (список ещё не
        # расшарен, неверный пароль и т.п.) не должен ронять саму запись задачи.
        logger.exception("Failed to mirror task to iCloud Reminders")
    await message.answer(f"✅ Задача #{task.id} записана: «{title}»\nОткрыть доску: /board")


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    tasks = tasks_store.list_tasks(include_done=False)
    if not tasks:
        await message.answer("Незавершённых задач нет 🎉")
        return

    lines = []
    for task in tasks:
        claim = f" (взял: {task.claimed_by})" if task.claimed_by else ""
        testing = " [тестируется]" if task.status == "testing" else ""
        lines.append(f"#{task.id} {task.title}{testing}{claim}")
    await message.answer(
        "Незавершённые задачи:\n" + "\n".join(lines) + "\n\nОтметить выполненной: /done <номер>, или /board"
    )


@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /done 7 (номер из /tasks или доски)")
        return

    try:
        task = tasks_store.complete_task(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return

    if task.reminder_uid:
        try:
            reminders.complete_task(task.reminder_uid)
        except ReminderTaskNotFound:
            pass
        except Exception:
            logger.exception("Failed to mirror completion to iCloud Reminders")

    await message.answer(f"✅ Готово: «{task.title}»")


def _requester_identity(message: Message) -> tuple[str, int | None]:
    if not message.from_user:
        return "неизвестно", None
    return message.from_user.full_name, message.from_user.id


@dp.message(Command("claim"))
async def cmd_claim(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /claim 7 (номер из /tasks или доски)")
        return
    name, user_id = _requester_identity(message)
    try:
        task = tasks_store.claim_task(int(arg), name, user_id)
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🙋 Взял в работу: «{task.title}»")


@dp.message(Command("unclaim"))
async def cmd_unclaim(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /unclaim 7")
        return
    try:
        task = tasks_store.unclaim_task(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🔓 Отпустил: «{task.title}»")


@dp.message(Command("testing"))
async def cmd_testing(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /testing 7")
        return
    try:
        task = tasks_store.mark_testing(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"🧪 На тестировании: «{task.title}»")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject) -> None:
    # Отдельно от /done — "отменили" и "сделали" разные исходы для
    # недельного спринт-дайджеста (см. sprint_loop/build_sprint_digest).
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /cancel 7 (номер из /tasks или доски)")
        return
    try:
        task = tasks_store.cancel_task(int(arg))
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"❌ Отменено: «{task.title}» (вернуть в работу можно через доску /board)")


@dp.message(Command("mytasks"))
async def cmd_mytasks(message: Message) -> None:
    _, user_id = _requester_identity(message)
    mine = [t for t in tasks_store.list_tasks(include_done=False) if t.claimed_by_user_id == user_id]
    if not mine:
        await message.answer("У тебя сейчас нет задач в работе. Взять: /claim <номер> или /board")
        return
    lines = []
    for task in mine:
        testing = " [тестируется]" if task.status == "testing" else ""
        lines.append(f"#{task.id} {task.title}{testing}")
    await message.answer("Твои задачи в работе:\n" + "\n".join(lines))


@dp.message(Command("comment"))
async def cmd_comment(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: /comment 7 текст комментария")
        return
    name, _ = _requester_identity(message)
    try:
        task = tasks_store.add_comment(int(parts[0]), name, parts[1].strip())
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"💬 Комментарий добавлен к «{task.title}»")


@dp.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: /rename 7 новое название задачи")
        return
    try:
        task = tasks_store.rename_task(int(parts[0]), parts[1].strip())
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return
    await message.answer(f"✏️ Переименовано: «{task.title}»")


@dp.message(Command("board"))
async def cmd_board(message: Message) -> None:
    if not board_config.webapp_url:
        await message.answer(
            "Мини-апп с доской задач ещё не задеплоен на публичный https-адрес "
            "(WEBAPP_URL в .env) — как будет, здесь появится кнопка."
        )
        return
    if message.chat.type != "private":
        # Telegram открывает web_app-кнопки из инлайн-клавиатуры только в
        # личном чате с ботом — в группе кнопка не сработает.
        await message.answer("Открой доску в личке со мной — напиши мне /board напрямую.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть доску задач", web_app=WebAppInfo(url=board_config.webapp_url))]
        ]
    )
    await message.answer("Доска задач команды:", reply_markup=keyboard)


@dp.message(Command("remindnow"))
async def cmd_remind_now(message: Message) -> None:
    # Ручной триггер того же дайджеста, что шлёт reminder_loop раз в день —
    # чтобы проверить формат/содержание, не дожидаясь TEAM_REMINDER_HOUR.
    digest = build_reminder_digest(tasks_store.list_tasks(include_done=False))
    await message.answer(digest or "Открытых задач нет — напоминать не о чем 🎉")


def _seconds_until_next_reminder() -> float:
    now = datetime.now()
    target = now.replace(hour=config.reminder_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def reminder_loop() -> None:
    # Раз в сутки, в config.reminder_hour по местному времени машины —
    # дайджест открытых задач в TEAM_CHAT_ID. Не персистится и не защищено от
    # пропуска при рестарте бота ровно в момент отправки — это ежедневное
    # напоминание, не критичная нотификация, простой sleep-цикл достаточен.
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_reminder())
            digest = build_reminder_digest(tasks_store.list_tasks(include_done=False))
            if digest:
                await bot.send_message(config.team_chat_id, digest)
        except Exception:
            logger.exception("Reminder loop iteration failed")


# По просьбе Кирилла: команда хочет не только "сделали/отменили/перенесли"
# (см. shared/sprint_digest.py), но и оценку эффективности + план на
# следующую неделю, исходя из стратегических целей проекта. Это два
# конкретных документа верхнего уровня (не весь Docs/) — 1) сама стратегия
# (диагноз/policy/вехи-гейты), 2) ближайший операционный план с датами —
# оба небольшие (~5-9 КБ), читаем целиком, без topic-based урезания.
_STRATEGY_DOC_PATHS = ["strategy/1_Kubyshka_Strategy.md", "strategy/2_Kubyshka_Development_Plan.md"]


def _load_strategy_context() -> str:
    parts = []
    for rel_path in _STRATEGY_DOC_PATHS:
        full_path = Path(config.finassist_docs_path) / rel_path
        if full_path.exists():
            parts.append(f"[{rel_path}]\n{full_path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


_SPRINT_ANALYSIS_SYSTEM_PROMPT = (
    "Ты — аналитик команды разработки приложения «Кубышка». Тебе дают: что "
    "сделали/отменили/перенесли за прошедшую неделю на доске задач (с "
    "числами и % выполнения из закрытых задач), стратегические цели и "
    "ближайший план проекта (документы ниже — там конкретные числовые "
    "gate-метрики и даты), и выдержку из рабочего чата команды за неделю "
    "(может отсутствовать).\n"
    "ГЛАВНОЕ ТРЕБОВАНИЕ: оценка должна быть КОЛИЧЕСТВЕННОЙ, а не только "
    "качественной прозой. Ответь коротко, по-русски, без канцелярита и "
    "воды, в 2 блока:\n"
    "1) Оценка недели ЧИСЛОМ: поставь оценку эффективности недели по шкале "
    "1-10 (10 — неделя максимально продвинула к ближайшим "
    "gate-метрикам/срокам из документов) и обоснуй её конкретными цифрами — "
    "% выполнения из закрытых задач, темп относительно того, что нужно по "
    "плану, и т.д. Если в чате или задачах упоминаются реальные метрики "
    "продукта (D7/D30, MAU, конверсия онбординга и т.п.) — обязательно "
    "приведи их и сравни с целевыми значениями из стратегии; не пиши "
    "\"хорошо\"/\"плохо\" без опоры на цифры.\n"
    "2) Приоритеты на следующую неделю: 2-3 конкретных пункта, каждый по "
    "возможности с числом (сколько задач/дней осталось до ближайшего "
    "гейта, целевой %, дедлайн из плана) — не абстрактные лозунги.\n"
    "Учитывай сигналы из чата (решения, проблемы, договорённости), если они "
    "релевантны. Не выдумывай цифры и факты, которых нет в данных ниже — "
    "если конкретных чисел для какого-то вывода нет, так и скажи прямо "
    "(«нет данных, чтобы оценить численно»), а не заменяй их качественными "
    "формулировками."
)


def _build_full_sprint_report(since: datetime, now: datetime) -> str | None:
    # R-COST: механическая сводка (done/cancelled/still_open) — ноль вызовов
    # LLM (см. shared/sprint_digest.py). Один-единственный доп. вызов LLM —
    # только оценка+план поверх неё, и только если вообще есть что подводить
    # (см. digest is None ниже) — идле-неделя не тратит токены зря.
    done = tasks_store.list_done_since(since)
    cancelled = tasks_store.list_cancelled_since(since)
    still_open = tasks_store.list_tasks(include_done=False)
    period_label = f"{since:%d.%m}–{now:%d.%m}"
    digest = build_sprint_digest(done, cancelled, still_open, period_label=period_label)
    if not digest:
        return None
    try:
        chat_excerpt = format_for_prompt(messages_since(_CHAT_LOG_PATH, since))
        sync_docs_repos(config.docs_paths)
        strategy_context = _load_strategy_context()
        user_content = f"Итоги недели по доске:\n{digest}\n\n"
        if chat_excerpt:
            user_content += f"Выдержка из чата команды за неделю:\n{chat_excerpt}\n\n"
        user_content += f"Стратегия и план проекта:\n{strategy_context}"
        analysis = llm.chat(
            [
                {"role": "system", "content": _SPRINT_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=700,
        )
    except Exception:
        # Механическая сводка важнее — если анализ упал (нет ключа, сеть,
        # провайдер лёг), всё равно отправляем то, что посчитали бесплатно.
        logger.exception("Sprint analysis failed — sending mechanical digest only")
        return digest
    if not analysis:
        return digest
    return f"{digest}\n\n🧭 <b>Оценка недели и план:</b>\n{analysis}"


@dp.message(Command("sprintnow"))
async def cmd_sprint_now(message: Message) -> None:
    # Превью текущего периода спринта — НЕ продвигает границу (в отличие от
    # sprint_loop), чтобы проверка формата не сбивала реальный недельный цикл.
    since, now = current_sprint_period(board_config.sprint_state_path)
    digest = _build_full_sprint_report(since, now)
    await message.answer(digest or "За текущий период спринта пока пусто — нечего подводить 🎉")


def _seconds_until_next_sprint_boundary() -> float:
    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7  # Monday=0 ... Saturday=5
    target = (now + timedelta(days=days_until_saturday)).replace(hour=config.sprint_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def sprint_loop() -> None:
    # Раз в неделю, в субботу в config.sprint_hour по местному времени машины
    # — итоги спринта в TEAM_CHAT_ID. Граница периода продвигается ВСЕГДА
    # (даже если сводка пустой период и не отправляется) — иначе пустая
    # неделя задвоила бы период со следующей.
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_sprint_boundary())
            since, now = current_sprint_period(board_config.sprint_state_path)
            digest = _build_full_sprint_report(since, now)
            save_last_sprint_at(board_config.sprint_state_path, now)
            if digest:
                await bot.send_message(config.team_chat_id, digest)
        except Exception:
            logger.exception("Sprint loop iteration failed")


def _ask_llm(history_key: _HistoryKey, question: str, *, force_context: bool = False, role: RoleAgent | None = None) -> str:
    if not llm.config.api_key:
        # OPENAI_API_KEY не обязателен для старта бота (задачи/доска не
        # используют LLM), но без него ассистент отвечать не может — явная
        # ошибка пользователю лучше молчания.
        raise RuntimeError("Ассистент ещё не настроен: не задан OPENAI_API_KEY.")
    # R-COST: контекст грузим, только если он реально нужен — иначе на
    # "привет"/"спасибо" улетал бы тот же объём токенов, что на серьёзный
    # архитектурный вопрос. /ask и ролевые команды (/dev, /techlead, ...) —
    # явное намерение спросить по делу, форсируем контекст в обоих случаях.
    if role is not None or force_context or question_needs_project_context(question):
        sync_docs_repos(config.docs_paths)
        # Файлы роли (playbook_files) — ВСЕГДА в контексте ролевого вопроса,
        # это то, что делает ответ консультацией именно от этой роли, а не
        # просто другим вступительным текстом поверх обычного ассистента.
        extra_files = list(role.playbook_files) if role else []
        for filename in topic_context_files(question):
            if filename not in extra_files:
                extra_files.append(filename)
        context = load_project_context(config.docs_paths, max_chars=_CONTEXT_MAX_CHARS, extra_filenames=extra_files)
        persona = f"\n\n{role.persona_prompt}" if role else ""
        system_content = f"{_SYSTEM_PROMPT}{persona}\n\nКонтекст проекта:\n{context}"
    else:
        system_content = _SYSTEM_PROMPT

    history = list(_conversation_history[history_key])
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    answer = llm.chat(messages, max_tokens=_ANSWER_MAX_TOKENS)

    _conversation_history[history_key].append({"role": "user", "content": question})
    _conversation_history[history_key].append({"role": "assistant", "content": answer})
    return answer


def _assistant_error_message(exc: Exception) -> str:
    # Наши собственные сообщения (например, "не настроен OPENAI_API_KEY")
    # безопасно показать как есть. Остальное — сеть/провайдер/неожиданное —
    # логируем полностью, но в чат не тащим детали (могут содержать служебную
    # информацию о запросе), чтобы не палить внутренности бота в общем чате.
    logger.exception("Assistant call failed")
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Не получилось спросить ассистента — что-то не так на стороне LLM-провайдера. Попробуй ещё раз чуть позже."


def _reject_if_rate_limited(message: Message) -> bool:
    """True, если сообщение НАДО отклонить (лимит исчерпан) — уже отправляет
    юзеру объяснение сама, вызывающему коду останется просто return."""
    if _rate_limiter.allow(message.chat.id):
        return False
    return True


@dp.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Формат: /ask почему подушка блокирует цели")
        return
    if _reject_if_rate_limited(message):
        wait_min = round(_rate_limiter.seconds_until_available(message.chat.id) / 60)
        await message.answer(f"⏳ Лимит вопросов на этот час исчерпан. Попробуй через ~{wait_min} мин.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = _ask_llm(_history_key(message), question, force_context=True)
    except Exception as exc:
        await message.answer(f"⚠️ {_assistant_error_message(exc)}")
        return
    await message.answer(answer or "Не получилось сформулировать ответ.")


@dp.message(Command("roles"))
async def cmd_roles(message: Message) -> None:
    lines = [f"/{role.command} — {role.display_name}" for role in list_role_agents()]
    await message.answer(
        "Ролевые консультации (советуют от лица роли, опираясь на плейбук "
        "Авито + доки проекта — сами код не пишут и тесты не гоняют):\n"
        + "\n".join(lines)
        + "\n\nФормат: /dev стоит ли кэшировать этот запрос?"
    )


def _make_role_handler(role: RoleAgent):
    # Замыкание должно захватывать role ПО ЗНАЧЕНИЮ на момент вызова
    # _make_role_handler, а не по ссылке на переменную цикла регистрации —
    # иначе все 5 команд отвечали бы от лица последней роли из ROLE_AGENTS.
    async def handler(message: Message, command: CommandObject) -> None:
        question = (command.args or "").strip()
        if not question:
            await message.answer(f"Формат: /{role.command} <вопрос>")
            return
        if _reject_if_rate_limited(message):
            wait_min = round(_rate_limiter.seconds_until_available(message.chat.id) / 60)
            await message.answer(f"⏳ Лимит вопросов на этот час исчерпан. Попробуй через ~{wait_min} мин.")
            return
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            answer = _ask_llm(_history_key(message), question, role=role)
        except Exception as exc:
            await message.answer(f"⚠️ {_assistant_error_message(exc)}")
            return
        await message.answer(answer or "Не получилось сформулировать ответ.")

    return handler


for _role in list_role_agents():
    dp.message(Command(_role.command))(_make_role_handler(_role))
del _role


def _strip_mention(text: str) -> str:
    if not _bot_username:
        return text
    pattern = re.compile(re.escape(f"@{_bot_username}"), re.IGNORECASE)
    return pattern.sub("", text).strip()


def _is_reply_to_bot(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot.id)


def _mentions_bot(message: Message) -> bool:
    if not _bot_username or not message.text:
        return False
    return f"@{_bot_username.lower()}" in message.text.lower()


def _should_respond_as_assistant(message: Message) -> bool:
    if not message.text or message.text.startswith("/"):
        return False
    if message.from_user and message.from_user.is_bot:
        # Без этого два бота, отвечающие друг другу (наш ассистент + любой
        # другой бот в том же чате), могли бы уйти в бесконечный цикл
        # ответов — каждый круг это реальный оплаченный вызов LLM.
        return False
    # Личка — всегда диалог с ассистентом, обращение по имени избыточно.
    if message.chat.type == "private":
        return True
    # В группе — только если реально ОБРАТИЛИСЬ: ответили на сообщение бота
    # или упомянули его по имени. Отвечать на каждую реплику в общем чате
    # было бы шумно (та же логика, что и в channel_bot для чата обсуждения).
    return _is_reply_to_bot(message) or _mentions_bot(message)


@dp.message(F.func(_should_respond_as_assistant))
async def handle_assistant_message(message: Message) -> None:
    question = _strip_mention(message.text or "")
    if not question:
        await message.reply("Да? Спрашивай — я тут 🙂")
        return
    if _reject_if_rate_limited(message):
        wait_min = round(_rate_limiter.seconds_until_available(message.chat.id) / 60)
        await message.reply(f"⏳ Лимит вопросов на этот час исчерпан. Попробуй через ~{wait_min} мин.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = _ask_llm(_history_key(message), question)
    except Exception as exc:
        await message.answer(f"⚠️ {_assistant_error_message(exc)}")
        return
    await message.answer(answer or "Не получилось сформулировать ответ.")


_BOT_COMMANDS = [
    BotCommand(command="task", description="Записать задачу на доску"),
    BotCommand(command="tasks", description="Незавершённые задачи"),
    BotCommand(command="mytasks", description="Задачи, которые взял я"),
    BotCommand(command="claim", description="Взять задачу себе"),
    BotCommand(command="unclaim", description="Отпустить задачу"),
    BotCommand(command="testing", description="Отправить задачу на тестирование"),
    BotCommand(command="done", description="Отметить задачу выполненной"),
    BotCommand(command="cancel", description="Отметить задачу отменённой"),
    BotCommand(command="comment", description="Комментарий к задаче"),
    BotCommand(command="rename", description="Переименовать задачу"),
    BotCommand(command="board", description="Доска задач (мини-апп)"),
    BotCommand(command="remindnow", description="Дайджест открытых задач сейчас"),
    BotCommand(command="sprintnow", description="Превью итогов спринта"),
    BotCommand(command="ask", description="Спросить ассистента про проект"),
    BotCommand(command="id", description="ID текущего чата"),
    BotCommand(command="help", description="Список команд"),
]



async def main() -> None:
    me = await bot.get_me()
    global _bot_username
    _bot_username = me.username
    logger.info("Bot username resolved: @%s", _bot_username)
    # Без этого Telegram не показывает автодополнение команд при вводе "/".
    await bot.set_my_commands(_BOT_COMMANDS)
    if config.team_chat_id:
        asyncio.create_task(reminder_loop())
        asyncio.create_task(sprint_loop())
    else:
        logger.info("TEAM_CHAT_ID не задан — ежедневный дайджест и итоги спринта отключены (доступны вручную через /remindnow, /sprintnow)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
