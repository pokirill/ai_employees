from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("team_bot.metrics_digest")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class MetricsFetchError(Exception):
    """Не удалось получить данные с /admin — сеть, 401, таймаут и т.п."""


# 🚨 Таймаут был 15 секунд, и этого перестало хватать (проверено на живом проде
# 02.09.2026): `/admin/persons.json` отвечает 573 КБ за ~19 секунд и растёт вместе
# с базой. Дайджест падал на нём КАЖДЫЙ раз, а ошибка уходила в лог внутри
# `except` в цикле напоминаний — снаружи это выглядело как «бот молчит».
#
# 60 секунд — с запасом к нынешним девятнадцати. Правильное решение другое:
# пагинация или лёгкий агрегат на стороне бэкенда вместо выгрузки всех людей
# целиком. Пока его нет, таймаут не должен быть тем, что рвёт отчёт.
_SLOW_ENDPOINT_TIMEOUT = 60.0


async def _fetch_json(client: httpx.AsyncClient, base_url: str, path: str, auth: tuple[str, str]) -> dict:
    resp = await client.get(f"{base_url}{path}", auth=auth, timeout=_SLOW_ENDPOINT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def fetch_metrics(base_url: str, username: str, password: str) -> tuple[dict, list[dict]]:
    """Тянет /admin/dashboard.json (агрегаты) и /admin/persons.json (по людям).

    Оба вызова — один и тот же basic-auth, что и веб-админка. Бросает
    MetricsFetchError с понятным сообщением при сетевой ошибке/401, чтобы
    вызывающий код мог честно сообщить в чат вместо тихого падения.
    """
    auth = (username, password)
    try:
        async with httpx.AsyncClient() as client:
            # 🚨 Сначала `/admin/product.json` — продуктовые метрики (активность,
            # retention, воронка пейволла). `/admin/dashboard.json` когда-то отдавал
            # именно их, но сменил смысл на операционную сводку (`mart`, `traffic`,
            # `errors`), и дайджест с тех пор молча выходил с прочерками: ключи
            # `product` и `data_quality` в ответе просто исчезли, а код читает их
            # через `.get` и не падает. Найдено 02.09.2026.
            #
            # Фолбэк на старый адрес оставлен на время, пока бэкенд не выкачен:
            # 404 здесь значит «сервер старее бота», а не поломку.
            try:
                dashboard = await _fetch_json(client, base_url, "/admin/product.json", auth)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                logger.warning("Нет /admin/product.json — беру старый /admin/dashboard.json")
                dashboard = await _fetch_json(client, base_url, "/admin/dashboard.json", auth)
            persons = await _fetch_json(client, base_url, "/admin/persons.json", auth)
    except httpx.HTTPStatusError as exc:
        raise MetricsFetchError(f"Админка ответила {exc.response.status_code} — проверь ADMIN_USERNAME/ADMIN_PASSWORD") from exc
    except httpx.HTTPError as exc:
        # `httpx.ReadTimeout` печатается пустой строкой, и сообщение получалось
        # «Не достучался до … —» без причины. Подставляем тип, иначе разбор
        # начинается с гадания, что именно случилось.
        reason = str(exc) or type(exc).__name__
        raise MetricsFetchError(f"Не достучался до {base_url} — {reason}") from exc
    return dashboard, persons


def last_complete_day(now: datetime | None = None) -> date:
    """Сутки, которые только что закончились, по московскому времени.

    Нужна, потому что дайджест шлётся в 00:00 МСК: в этот момент «сегодня» —
    это новые сутки с нулём событий, и отчитываться надо за вчерашний день.
    """
    moment = now or datetime.now(MOSCOW_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW_TZ)
    return (moment.astimezone(MOSCOW_TZ) - timedelta(days=1)).date()


def _metric_line(entry: dict) -> str:
    status_icon = {"ok": "✅", "warn": "⚠️", "neutral": "•"}.get(entry.get("status"), "•")
    label = html.escape(str(entry["label"]))
    value = html.escape(str(entry["value"]))
    target = html.escape(str(entry["target"]))
    return f"{status_icon} {label}: {value} (цель {target})"


def _activity_for_day(product: dict, report_date: date) -> tuple[dict, bool]:
    """Берёт срез активности ЗА КОНКРЕТНЫЙ день из series.

    Раньше брали `latest` — то есть всегда «сегодня» по МСК. При отправке в
    полночь это давало DAU ≈ 0, потому что новые сутки только начались.
    Возвращает (срез, найден_ли_точный_день); если точного дня нет, отдаём
    `latest` и вызывающий код честно пишет, что день не тот.
    """
    active_users = product.get("active_users", {}) or {}
    wanted = report_date.isoformat()
    for entry in active_users.get("series") or []:
        if entry.get("date") == wanted:
            return entry, True
    return active_users.get("latest", {}) or {}, False


def build_metrics_digest(
    dashboard: dict,
    persons: list[dict],
    *,
    report_date: date | None = None,
    partial_day: bool = False,
) -> str:
    """Собирает текстовый дайджест для команды из сырых JSON-ответов админки.

    Специально НЕ вызывает LLM (в отличие от sprint-отчёта) — все числа уже
    посчитаны бэкендом (dashboard.json) или считаются тривиальным подсчётом
    списка (persons.json), выдумывать/интерпретировать нечего, а ежедневный
    дайджест не должен зависеть от LLM-бюджета/аптайма.

    `report_date` — за какие сутки отчитываемся (по МСК). `partial_day=True`
    означает «сутки ещё не закончились» (ручной вызов /metricsnow) — это
    пишется в заголовке, чтобы неполные числа не выглядели как итоговые.

    Язык осознанно без жаргона: отчёт читают основатели, а не аналитики.
    Термин «paywall» уже приводил к вопросу «что же такое пейвол?» —
    поэтому здесь только человеческие формулировки, а расшифровки лежат
    в /terms (build_glossary).
    """
    report_date = report_date or last_complete_day()
    date_str = report_date.isoformat()

    dq = dashboard.get("data_quality", {})
    product = dashboard.get("product", {})
    activity, exact_day = _activity_for_day(product, report_date)
    retention = product.get("retention", {})
    paywall = product.get("paywall", {})
    metrics = product.get("metrics", [])

    new_that_day = sum(1 for p in persons if (p.get("first_seen_at") or "").startswith(date_str))

    header_date = report_date.strftime("%d.%m.%Y")
    if partial_day:
        head = f"🐹 Кубыши, привет! Метрики за неполные сутки {header_date} (день ещё идёт):"
    else:
        head = f"🐹 Кубыши, привет! Отчёт по метрикам за {header_date}:"
    lines: list[str] = [head, ""]

    # ВАЖНО: показываем ОДНО число активности за день, а не два похожих рядом.
    # Раньше здесь же печаталось «N активны за 24ч» из persons.json — это другое
    # определение (скользящее окно 24ч по UTC и ЛЮБОЕ событие против календарных
    # суток МСК и только осмысленных действий). Два похожих числа рядом
    # регулярно читались как расхождение в расчётах, хотя считали разное.
    lines.append(
        "Заходили в приложение: {dau} чел. · за неделю {wau} · за месяц {mau}".format(
            dau=activity.get("dau", "—"),
            wau=activity.get("wau", "—"),
            mau=activity.get("mau", "—"),
        )
    )
    if not exact_day:
        lines.append("⚠️ Данных ровно за эти сутки в админке нет — показан последний доступный день.")
    lines.append(
        f"Всего людей знаем: {dq.get('known_people', '—')} (+{new_that_day} впервые за этот день)"
    )
    lines.append("")

    if metrics:
        ok_count = sum(1 for m in metrics if m.get("status") == "ok")
        pct = round(100 * ok_count / len(metrics))
        lines.append(f"🎉 Вы молодцы! Уже {pct}% ключевых метрик ({ok_count} из {len(metrics)}) в зелёной зоне.")
        lines.append("")
        lines.append("<b>Ключевые метрики:</b>")
        for entry in metrics:
            lines.append(_metric_line(entry))
        lines.append("")

    checkpoints = {c["day"]: html.escape(str(c["rate"])) for c in retention.get("checkpoints", [])}
    if checkpoints:
        wanted = ["D1", "D7", "D14", "D30"]
        parts = [f"{d} {checkpoints[d]}" for d in wanted if d in checkpoints and checkpoints[d] != "—"]
        if parts:
            lines.append(f"Возвращаются в приложение: {' · '.join(parts)}")
            lines.append("")

    if paywall:
        # Без слова «paywall» и без «триала» — те же числа человеческим языком.
        lines.append(
            "Оплата: экран подписки видели {shown} чел. · пробный период включили {trial} ({conv}) · оплатили {purchases}".format(
                shown=paywall.get("shown", 0),
                trial=paywall.get("trial_accepted", 0),
                conv=html.escape(str(paywall.get("trial_conversion", "—"))),
                purchases=paywall.get("purchase_completed", 0),
            )
        )
        lines.append("")

    warn_risks = [r for r in product.get("launch_risks", []) if r.get("status") == "warn"]
    if warn_risks:
        lines.append("<b>Стоит поднажать по этим вещам:</b>")
        for risk in warn_risks[:3]:
            risk_text = html.escape(str(risk["risk"]))
            metric_text = html.escape(str(risk.get("metric", "")))
            lines.append(f"⚠️ {risk_text} — {metric_text}")
        lines.append("")

    lines.append("<i>Непонятен термин — /terms</i>")

    return "\n".join(lines).strip()


GLOSSARY = [
    (
        "Заходили в приложение (DAU/WAU/MAU)",
        "Сколько РАЗНЫХ людей открывали приложение за сутки / 7 дней / 30 дней. "
        "Считается по календарным суткам московского времени и только по осмысленным "
        "действиям: открыл приложение, начал сессию, посмотрел экран. Один человек "
        "за день считается один раз, сколько бы он ни заходил.",
    ),
    (
        "Почему это число не совпадает с «активными за 24 часа» в админке",
        "Потому что это два разных измерения, а не ошибка расчёта. «Заходили» — "
        "календарные сутки МСК и только осмысленные действия. «Активные за 24 часа» "
        "в блоке качества данных — скользящее окно последних 24 часов и ЛЮБОЕ событие "
        "от устройства, включая техническое. Поэтому второе почти всегда больше. "
        "В этом отчёте специально показывается только первое, чтобы не путаться.",
    ),
    (
        "Экран подписки (раньше писали «paywall»)",
        "Экран, где предлагается оформить подписку. «Видели» — сколько людей до него "
        "дошло. «Пробный период включили» — сколько согласились на бесплатный триал. "
        "«Оплатили» — сколько реально заплатили деньги. Согласие на пробный период "
        "НЕ равно оплате: это разные шаги, и путать их нельзя.",
    ),
    (
        "Возвращаются в приложение (Retention D1/D7/D30)",
        "Доля людей, которые вернулись через 1, 7 или 30 дней после первого дня. "
        "D30 — главная цель (нужно ≥25%). Считается по когортам: в знаменателе только "
        "те, кто УЖЕ дожил до этого дня, поэтому свежие пользователи не портят картину.",
    ),
    (
        "Активация 48ч",
        "Доля людей, которые за первые 48 часов записали хотя бы 3 траты. Это признак, "
        "что человек реально начал пользоваться, а не просто зарегистрировался. Цель ≥40%.",
    ),
    (
        "Конверсия онбординга",
        "Доля тех, кто дошёл до конца первичной настройки от тех, кто её начал. Цель ≥15%.",
    ),
    (
        "TTV",
        "Time To Value — сколько времени проходит от запуска до первой пользы "
        "(зафиксированный план). Цель — меньше 3 минут.",
    ),
    (
        "Всего людей знаем",
        "Сколько РАЗНЫХ людей вообще видел бэкенд (склеенных по person_id, а не "
        "установок: одна и та же переустановка не считается новым человеком). "
        "Внутренний трафик — симуляторы и наши тестовые прогоны — исключён.",
    ),
]


def build_glossary() -> str:
    """Расшифровки терминов из дайджеста человеческим языком.

    Появилось после вопроса основателя «Что же такое пейвол?» — если термин
    в отчёте требует расшифровки, она должна быть в одном тапе, а не в чужой
    голове. Отдельным пунктом объясняет, почему «заходили в приложение» и
    «активные за 24 часа» в админке не сходятся: это разные измерения, а не
    ошибка расчёта (тот же вопрос уже задавался и про веб-админку).
    """
    lines = ["📖 <b>Что значат слова в отчёте</b>", ""]
    for term, explanation in GLOSSARY:
        lines.append(f"<b>{html.escape(term)}</b>")
        lines.append(html.escape(explanation))
        lines.append("")
    return "\n".join(lines).strip()
