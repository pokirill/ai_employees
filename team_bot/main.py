from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from docx import Document

from shared.chat_log import DEFAULT_LOG_PATH, append_chat_message, format_for_prompt, messages_since
from shared.config import LLMConfig, TaskBoardConfig, TeamBotConfig
from shared.context_heuristic import question_needs_project_context
from shared.docs_context import load_project_context, sync_docs_repos, topic_context_files
from shared.icloud_reminders import ICloudReminders
from shared.icloud_reminders import TaskNotFound as ReminderTaskNotFound
from shared.llm_client import LLMClient
from shared.metrics_digest import MetricsFetchError, build_metrics_digest, fetch_metrics
from shared.rate_limiter import SlidingWindowLimiter
from shared.reminder_digest import build_reminder_digest
from shared.role_agents import RoleAgent, list_role_agents, role_agent_for_command
from shared.sprint_digest import build_sprint_digest
from shared.sprint_state import current_sprint_period, save_last_sprint_at
from shared.task_store import TaskNotFound, TaskStore
from shared.transcription_client import MeetingTranscript, TranscriptionClient

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

# Фото, прикреплённые к задачам (см. cmd_photo/_save_task_photo) — лежат под
# webapp/static по умолчанию, чтобы webapp/server.py отдавал их без отдельного
# роута (там уже смонтирован StaticFiles на /static).
_TASK_PHOTOS_DIR = Path(board_config.photos_dir)
_TASK_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


async def _save_task_photo(source: Message, task_id: int, added_by: str, caption: str = "") -> bool:
    """Качает наибольшее разрешение фото из source.photo и прикрепляет к задаче
    task_id. Вызывающий уже проверил, что source.photo не пусто."""
    if not source.photo:
        return False
    largest = source.photo[-1]
    file_name = f"{task_id}_{uuid.uuid4().hex}.jpg"
    photo_path = _TASK_PHOTOS_DIR / file_name
    try:
        await bot.download(largest.file_id, destination=str(photo_path))
    except Exception:
        logger.exception("Failed to download task photo")
        return False
    tasks_store.add_photo(task_id, file_name, added_by=added_by, caption=caption)
    # Анализ фото (vision + контекст из доков проекта) не должен задерживать
    # ответ "фото прикреплено" — LLM-запросы занимают секунды, а сам факт
    # прикрепления пользователь должен увидеть сразу. Дописывается в описание
    # задачи в фоне, best-effort (см. _analyze_and_annotate_photo).
    asyncio.create_task(_analyze_and_annotate_photo(task_id, str(photo_path), source.chat.id))
    return True


async def _analyze_and_annotate_photo(task_id: int, photo_path: str, chat_id: int) -> None:
    """Фоновый анализ прикреплённого к задаче фото: vision-описание того, что
    на скриншоте, плюс релевантный (по теме описания) контекст из BACKLOG/
    AI_CHANGELOG обоих репо — дописывается в description задачи, чтобы
    карточка на доске сама объясняла суть фото и её связь с текущим
    состоянием проекта, без ручного пересказа человеком. Best-effort: сбой
    LLM/файла тихо логируется, не портит уже прикреплённое фото."""
    try:
        image_description = await asyncio.to_thread(
            llm.describe_image,
            photo_path,
            "Кратко (2-3 предложения, по-русски, без markdown) опиши, что на этом "
            "скриншоте: какой экран/фича мобильного приложения видна, есть ли "
            "заметная проблема или баг.",
            max_tokens=250,
        )
        if not image_description.strip():
            return

        extra_files = topic_context_files(image_description)
        project_context = load_project_context(config.docs_paths, max_chars=6000, extra_filenames=extra_files)

        summary = await asyncio.to_thread(
            llm.chat,
            [
                {
                    "role": "system",
                    "content": (
                        "Дополни описание задачи на доске команды. По анализу скриншота и "
                        "текущему состоянию проекта (беклог/доки ниже) напиши короткое "
                        "(3-5 предложений) дополнение: что видно на фото и как это соотносится "
                        "с текущим кодом/беклогом, если есть связь (сослись на конкретный пункт "
                        "беклога, если найдёшь). Если связи нет — просто перескажи, что на фото. "
                        "По-русски, без markdown, по делу, без вступлений."
                    ),
                },
                {"role": "user", "content": f"Анализ фото:\n{image_description}\n\nКонтекст проекта:\n{project_context}"},
            ],
            max_tokens=350,
        )
        if not summary.strip():
            return

        task = tasks_store.get_task(task_id)
        addition = f"📷 Авто-анализ фото:\n{summary.strip()}"
        new_description = f"{task.description}\n\n{addition}" if task.description else addition
        tasks_store.set_description(task_id, new_description)
        await bot.send_message(chat_id, f"🔍 Добавил в задачу #{task_id} контекст по фото — см. описание в /board.")
    except TaskNotFound:
        pass
    except Exception:
        logger.exception("Photo analysis failed for task #%s", task_id)

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

# Транскрибация встреч (см. handle_meeting_recording ниже) — своя, отдельная
# от _ANSWER_MAX_TOKENS константа: структурированное резюме встречи (JSON,
# см. _MEETING_SUMMARY_PROMPT) занимает больше, чем ответ на разовый вопрос.
_MEETING_SUMMARY_MAX_TOKENS = 1200
_MEETING_TRANSCRIPT_MAX_CHARS = 40_000
# Просим строго JSON (не markdown/текст) — это даёт: (1) разделы резюме,
# которые реально можно отрисовать по отдельности (темы/решения/задачи/
# открытые вопросы), а не угадывать их в сплошном тексте; (2) action_items как
# структуру, которую можно сразу завести на доску задач (см.
# handle_meeting_recording — tasks_store.add_task), а не просто напечатать.
_MEETING_SUMMARY_PROMPT = (
    "Ты помогаешь команде разобрать транскрипт встречи (реплики размечены таймкодом и "
    "спикером — если диаризации не было, весь текст одним потоком без спикеров). Ответь "
    "СТРОГО валидным JSON без markdown-разметки, code fences и пояснений вокруг, по схеме:\n"
    '{"tldr": "1-2 предложения о сути встречи", '
    '"topics": ["тема 1", "тема 2"], '
    '"decisions": ["принятое решение 1"], '
    '"action_items": [{"assignee": "имя, ТОЛЬКО если оно явно прозвучало в разговоре, иначе null", '
    '"task": "что именно сделать"}], '
    '"open_questions": ["упомянули, но не решили"]}\n'
    "Не выдумывай имена ответственных — если из реплик не ясно, кто именно, assignee: null. "
    "Если по разделу нечего сказать — пустой список, не выдумывай содержание."
)
# Стандартный (облачный) Telegram Bot API не отдаёт боту файлы больше этого —
# для больших записей встреч нужен свой Local Bot API Server, не входит в
# текущий охват задачи.
_TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB = 20

bot = Bot(token=config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
# NEXARA_API_KEY не обязателен для старта бота — как и OPENAI_API_KEY у
# ассистента, отсутствие ключа просто отключает эту конкретную возможность
# вместо падения при импорте модуля.
transcription_client = TranscriptionClient(config.nexara_api_key) if config.nexara_api_key else None


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
        "/photo &lt;номер&gt; — без фото в сообщении: пришлёт уже прикреплённые к задаче фото. Чтобы прикрепить новое: пришли фото с подписью «/photo номер» (или ответь этой командой на фото)\n"
        "/board — открыть доску задач (мини-апп: карточка, комментарии, статус) — в личке\n"
        "/remindnow — прислать дайджест открытых задач сейчас (обычно раз в день сам)\n"
        f"/sprintnow — превью итогов спринта за текущий период (сам — по субботам в {config.sprint_hour}:00)\n"
        f"/metricsnow — дайджест продуктовых метрик сейчас (сам — каждый вечер в {config.metrics_hour}:00)\n\n"
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

    photo_note = ""
    # Заводили задачу с фото — либо ответом на фото, либо (проще) прислав
    # само фото с подписью "/task текст" одним сообщением — прикрепим его сразу,
    # не только подпись как заголовок.
    photo_source = message if message.photo else message.reply_to_message
    if photo_source and photo_source.photo:
        if await _save_task_photo(photo_source, task.id, added_by=author):
            photo_note = "\n📷 Фото прикреплено."
        else:
            photo_note = "\n⚠️ Не получилось прикрепить фото — попробуй /photo позже."
    await message.answer(f"✅ Задача #{task.id} записана: «{title}»{photo_note}\nОткрыть доску: /board")


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


@dp.message(Command("photo"))
async def cmd_photo(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(
            "Формат: /photo 7 — пришлёт фото, уже прикреплённые к задаче #7.\n"
            "Чтобы ПРИКРЕПИТЬ новое: пришли фото с подписью «/photo 7» (или ответь "
            "этой командой на сообщение с фото)."
        )
        return
    task_id = int(arg)
    try:
        task = tasks_store.get_task(task_id)
    except TaskNotFound:
        await message.answer("⚠️ Задача с таким номером не найдена — проверь /tasks.")
        return

    # Проще всего — фото прямо в ЭТОМ сообщении, командой в подписи (не нужно
    # отправлять фото отдельно и потом отвечать на него). Реплай на отдельное
    # фото-сообщение по-прежнему работает, для обратной совместимости.
    photo_source = message if message.photo else message.reply_to_message
    if not photo_source or not photo_source.photo:
        # Нет фото в этом сообщении — читаем команду как запрос "покажи
        # прикреплённые фото", а не "прикрепи новое".
        if not task.photos:
            await message.answer(
                f"К задаче #{task_id} фото не прикреплено. Чтобы прикрепить — пришли "
                f"фото с подписью «/photo {task_id}»."
            )
            return
        for photo in task.photos:
            photo_file = _TASK_PHOTOS_DIR / photo.file_name
            if not photo_file.exists():
                continue
            await message.answer_photo(FSInputFile(photo_file), caption=photo.caption or None)
        return

    name, _ = _requester_identity(message)
    # Если фото — само это сообщение, caption это ЖЕ команда ("/photo 7"), не
    # осмысленное описание — не сохраняем её как caption фото.
    photo_caption = "" if photo_source is message else (photo_source.caption or "")
    if not await _save_task_photo(photo_source, task_id, added_by=name, caption=photo_caption):
        await message.answer("⚠️ Не получилось скачать фото — попробуй ещё раз.")
        return
    await message.answer(f"📷 Фото прикреплено к задаче #{task_id}. Открыть доску: /board")


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
    # Кэш-бастинг: Telegram кэширует мини-апп по URL кнопки — если открывать
    # ВСЕГДА один и тот же board_config.webapp_url, WebView может годами
    # показывать версию index.html, загруженную при самом первом открытии,
    # игнорируя и Cache-Control, и реальные обновления фронтенда (см.
    # WEBAPP-CACHE-1 в BACKLOG). Уникальный query-параметр на каждый /board —
    # это для Telegram уже "другой" URL, поэтому WebView грузит его с нуля.
    fresh_url = f"{board_config.webapp_url}?v={int(datetime.now(timezone.utc).timestamp())}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📋 Открыть доску задач", web_app=WebAppInfo(url=fresh_url))]]
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


async def _build_metrics_digest_or_error() -> str:
    if not (config.admin_username and config.admin_password):
        return "Дайджест метрик не настроен: не заданы ADMIN_USERNAME/ADMIN_PASSWORD."
    try:
        dashboard, persons = await fetch_metrics(config.admin_base_url, config.admin_username, config.admin_password)
    except MetricsFetchError as exc:
        logger.exception("Metrics fetch failed")
        return f"Не удалось получить метрики: {exc}"
    return build_metrics_digest(dashboard, persons)


@dp.message(Command("metricsnow"))
async def cmd_metrics_now(message: Message) -> None:
    # Ручной триггер того же дайджеста, что шлёт metrics_loop раз в день —
    # чтобы проверить формат, не дожидаясь TEAM_METRICS_HOUR.
    await message.answer(await _build_metrics_digest_or_error())


def _seconds_until_next_metrics() -> float:
    now = datetime.now()
    target = now.replace(hour=config.metrics_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def metrics_loop() -> None:
    # Раз в сутки, в config.metrics_hour ("вечером") по местному времени
    # машины — дайджест продуктовых метрик (DAU/WAU/MAU, retention, воронка,
    # paywall) с /admin/dashboard.json + /admin/persons.json в TEAM_CHAT_ID.
    # Та же простая sleep-петля, что reminder_loop/sprint_loop — не критичная
    # нотификация, персистентность пропуска при рестарте не нужна.
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_metrics())
            await bot.send_message(config.team_chat_id, await _build_metrics_digest_or_error())
        except Exception:
            logger.exception("Metrics loop iteration failed")


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


def _meeting_recording_file(message: Message) -> tuple[str, int | None] | None:
    """file_id + file_size для голосового/аудио/видео/видео-кружка, а для
    обычного файла (document) — только если это явно аудио/видео (иначе
    хендлер перехватывал бы вообще любую пересланную картинку/pdf)."""
    if message.voice:
        return message.voice.file_id, message.voice.file_size
    if message.audio:
        return message.audio.file_id, message.audio.file_size
    if message.video:
        return message.video.file_id, message.video.file_size
    if message.video_note:
        return message.video_note.file_id, message.video_note.file_size
    if message.document and (message.document.mime_type or "").startswith(("audio/", "video/")):
        return message.document.file_id, message.document.file_size
    return None


def _is_meeting_recording(message: Message) -> bool:
    return _meeting_recording_file(message) is not None


# Готовый транскрипт текстом (человек сам расшифровал встречу, например в
# другом сервисе, и прислал файл) — в отличие от _meeting_recording_file
# (аудио/видео, которое ЕЩЁ предстоит транскрибировать нам самим).
_MEETING_TRANSCRIPT_DOCUMENT_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "text/plain",  # .txt
}
_MEETING_TRANSCRIPT_DOCUMENT_EXTENSIONS = (".docx", ".txt")


def _meeting_transcript_document_file(message: Message) -> tuple[str, int | None, str] | None:
    """file_id + размер + расширение для документа с готовой текстовой
    транскрибацией (.docx/.txt). Проверяем и mime_type, и расширение имени
    файла — некоторые клиенты Telegram шлют .docx с generic
    application/octet-stream вместо честного mime-типа."""
    if not message.document:
        return None
    filename = message.document.file_name or ""
    mime = message.document.mime_type or ""
    ext = Path(filename).suffix.lower()
    if ext not in _MEETING_TRANSCRIPT_DOCUMENT_EXTENSIONS and mime not in _MEETING_TRANSCRIPT_DOCUMENT_MIME_TYPES:
        return None
    if not ext:
        ext = ".docx" if "wordprocessingml" in mime else ".txt"
    return message.document.file_id, message.document.file_size, ext


def _is_meeting_transcript_document(message: Message) -> bool:
    return _meeting_transcript_document_file(message) is not None


def _extract_transcript_text(path: str, ext: str) -> str:
    """.docx — через python-docx (текст параграфов, без таблиц/сносок — этого
    достаточно для транскрипта встречи); .txt — сырые байты с fallback по
    кодировке (Word/Блокнот на Windows нередко сохраняют в cp1251)."""
    if ext == ".docx":
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    data = Path(path).read_bytes()
    for encoding in ("utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def _transcribe_recording(path: str) -> MeetingTranscript:
    """Nexara (диаризация по спикерам, лимит 3 ГБ), если задан NEXARA_API_KEY —
    иначе OpenAI Whisper на уже настроенном OPENAI_API_KEY (без спикеров, но
    без дополнительной регистрации). llm.transcribe — синхронный вызов, гоним
    его в отдельном потоке, чтобы не морозить event loop бота на время всего
    запроса к OpenAI."""
    if transcription_client is not None:
        return await transcription_client.transcribe(path)
    text = await asyncio.to_thread(llm.transcribe, path)
    return MeetingTranscript(text=text)


def _parse_meeting_summary(raw: str) -> dict | None:
    """LLM просят строгий JSON, но модели периодически всё равно оборачивают
    ответ в ```json ... ``` — снимаем такую обёртку защитно перед парсингом,
    вместо того чтобы считать это поломанным ответом."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _meeting_action_item_title(item: dict) -> str | None:
    task = str(item.get("task") or "").strip()
    if not task:
        return None
    assignee = item.get("assignee")
    return f"{assignee} — {task}" if assignee else task


def _create_task_from_meeting(title: str) -> None:
    """Тот же путь, что и ручной /task (доска — источник правды, Напоминания —
    best-effort зеркало) — см. cmd_task выше. created_by="встреча" — чтобы на
    доске было видно, что задача пришла из автоматического резюме, а не
    вручную от конкретного человека."""
    task = tasks_store.add_task(title, created_by="встреча")
    try:
        uid = reminders.add_task(title, notes=f"Из резюме встречи, доска #{task.id}")
        tasks_store.set_reminder_uid(task.id, uid)
    except Exception:
        logger.exception("Failed to mirror meeting task to iCloud Reminders")


def _format_meeting_digest(data: dict, transcript: MeetingTranscript, message_date: datetime) -> tuple[str, list[str]]:
    """Возвращает (текст резюме для Telegram, заголовки заведённых на доску
    задач) — заголовки нужны отдельно, чтобы вызывающий код мог сам решить,
    заводить ли их как настоящие задачи (см. handle_meeting_recording)."""
    meta_bits = [message_date.strftime("%d.%m")]
    if transcript.duration_seconds:
        meta_bits.append(f"{round(transcript.duration_seconds / 60)} мин")
    if transcript.speaker_count:
        meta_bits.append(f"участников: {transcript.speaker_count}")
    lines = [f"📋 <b>Резюме встречи</b> · {', '.join(meta_bits)}", ""]

    tldr = str(data.get("tldr") or "").strip()
    if tldr:
        lines.append(tldr)
        lines.append("")

    def _bullets(title: str, items: list) -> None:
        clean = [str(item).strip() for item in items if str(item).strip()]
        if not clean:
            return
        lines.append(f"<b>{title}:</b>")
        lines.extend(f"• {item}" for item in clean)
        lines.append("")

    _bullets("Темы", data.get("topics") or [])
    _bullets("Решения", data.get("decisions") or [])

    action_titles = []
    for item in data.get("action_items") or []:
        if not isinstance(item, dict):
            continue
        title = _meeting_action_item_title(item)
        if title:
            action_titles.append(title)
    _bullets("Задачи", action_titles)
    _bullets("Открыто (без ответа)", data.get("open_questions") or [])

    return "\n".join(lines).strip(), action_titles


async def _summarize_and_deliver_meeting_transcript(
    message: Message, transcript: MeetingTranscript, tmp_dir: str, status: Message
) -> None:
    """Общий хвост для обоих источников транскрипта (аудио, которое мы сами
    транскрибировали, ИЛИ уже готовый .docx/.txt) — от текста транскрипта до
    резюме в чат, задач на доске и файла с полным текстом. Один и тот же
    контракт делает баги в этом месте видимыми сразу для обоих путей, а не
    только для того, который тестировали последним."""
    if not transcript.text.strip():
        await status.edit_text("Транскрипт пустой — нечего разбирать.")
        return

    await status.edit_text("✍️ Готовлю резюме...")
    summary_data = None
    try:
        raw_summary = llm.chat(
            [
                {"role": "system", "content": _MEETING_SUMMARY_PROMPT},
                {"role": "user", "content": transcript.text[:_MEETING_TRANSCRIPT_MAX_CHARS]},
            ],
            max_tokens=_MEETING_SUMMARY_MAX_TOKENS,
        )
        summary_data = _parse_meeting_summary(raw_summary) if raw_summary else None
    except Exception:
        logger.exception("Meeting summary LLM call failed")

    transcript_path = Path(tmp_dir) / "transcript.txt"
    transcript_path.write_text(transcript.text, encoding="utf-8")

    await status.delete()

    action_titles: list[str] = []
    if summary_data is not None:
        digest_text, action_titles = _format_meeting_digest(summary_data, transcript, message.date)
        await message.reply(digest_text)
    else:
        # JSON не распарсился (или LLM вообще не ответил) — не выбрасываем
        # работу транскрибации впустую, отдаём хотя бы файл с транскриптом.
        logger.warning("Meeting summary was not valid JSON, skipping digest")
        await message.reply("Не получилось разобрать резюме встречи — но вот полный транскрипт файлом.")

    for title in action_titles:
        _create_task_from_meeting(title)
    if action_titles:
        await message.reply(f"✅ Добавлено на доску и в напоминания: {len(action_titles)} задач(и). /board")

    await message.reply_document(
        FSInputFile(transcript_path, filename="transcript.txt"),
        caption="Полный текст встречи",
    )


@dp.message(F.func(_is_meeting_recording))
async def handle_meeting_recording(message: Message) -> None:
    if transcription_client is None and not llm.config.api_key:
        await message.reply(
            "Транскрибация записей встреч ещё не настроена — не задан ни NEXARA_API_KEY, ни OPENAI_API_KEY."
        )
        return

    file_id, file_size = _meeting_recording_file(message)  # type: ignore[misc]
    if file_size and file_size > _TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB * 1024 * 1024:
        await message.reply(
            f"Файл больше {_TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB} МБ — обычный Telegram Bot API "
            "не даёт его скачать. Пришли запись как аудио, а не видео (звук легче), либо разбей на части."
        )
        return

    status = await message.reply("🎙 Транскрибирую запись, это может занять пару минут...")
    tmp_dir = tempfile.mkdtemp(prefix="meeting_")
    try:
        recording_path = str(Path(tmp_dir) / "recording")
        try:
            await bot.download(file_id, destination=recording_path, timeout=300)
        except Exception:
            logger.exception("Failed to download meeting recording")
            await status.edit_text("Не получилось скачать файл — Telegram мог отказать из-за размера.")
            return

        await bot.send_chat_action(message.chat.id, "typing")
        try:
            transcript = await _transcribe_recording(recording_path)
        except Exception:
            logger.exception("Meeting transcription failed")
            await status.edit_text("Не получилось транскрибировать — сервис транскрибации ответил ошибкой.")
            return

        if not transcript.text.strip():
            await status.edit_text("Транскрибация вернула пустой текст — похоже, в записи нет речи.")
            return

        await _summarize_and_deliver_meeting_transcript(message, transcript, tmp_dir, status)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dp.message(F.func(_is_meeting_transcript_document))
async def handle_meeting_transcript_document(message: Message) -> None:
    """Пользователь сам расшифровал встречу (например, другим сервисом) и
    прислал готовый текст файлом .docx/.txt — тот же результат (резюме,
    задачи на доске), что и для аудио, но без шага транскрибации: текст
    уже есть, экономим время и деньги на Nexara/Whisper."""
    if not llm.config.api_key:
        await message.reply("Резюме по готовому транскрипту требует настроенного LLM (OPENAI_API_KEY), а его нет.")
        return

    file_id, file_size, ext = _meeting_transcript_document_file(message)  # type: ignore[misc]
    if file_size and file_size > _TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB * 1024 * 1024:
        await message.reply(f"Файл больше {_TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB} МБ — Telegram Bot API не даёт его скачать.")
        return

    status = await message.reply("📄 Читаю транскрипт...")
    tmp_dir = tempfile.mkdtemp(prefix="meeting_doc_")
    try:
        doc_path = str(Path(tmp_dir) / f"transcript{ext}")
        try:
            await bot.download(file_id, destination=doc_path, timeout=120)
        except Exception:
            logger.exception("Failed to download meeting transcript document")
            await status.edit_text("Не получилось скачать файл.")
            return

        try:
            text = _extract_transcript_text(doc_path, ext)
        except Exception:
            logger.exception("Failed to extract text from meeting transcript document")
            await status.edit_text("Не получилось прочитать файл — убедись, что это .docx или .txt с текстом транскрипта.")
            return

        transcript = MeetingTranscript(text=text)
        await _summarize_and_deliver_meeting_transcript(message, transcript, tmp_dir, status)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    BotCommand(command="photo", description="Прикрепить фото к задаче или показать уже прикреплённые"),
    BotCommand(command="board", description="Доска задач (мини-апп)"),
    BotCommand(command="remindnow", description="Дайджест открытых задач сейчас"),
    BotCommand(command="sprintnow", description="Превью итогов спринта"),
    BotCommand(command="metricsnow", description="Дайджест продуктовых метрик сейчас"),
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
        asyncio.create_task(metrics_loop())
    else:
        logger.info("TEAM_CHAT_ID не задан — ежедневный дайджест, итоги спринта и метрики отключены (доступны вручную через /remindnow, /sprintnow, /metricsnow)")
    if not (config.admin_username and config.admin_password):
        logger.info("ADMIN_USERNAME/ADMIN_PASSWORD не заданы — ежевечерний дайджест метрик будет отвечать честной ошибкой вместо данных")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
