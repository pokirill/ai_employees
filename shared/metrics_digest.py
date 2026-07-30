from __future__ import annotations

import html
import logging
from datetime import date

import httpx

logger = logging.getLogger("team_bot.metrics_digest")


class MetricsFetchError(Exception):
    """Не удалось получить данные с /admin — сеть, 401, таймаут и т.п."""


async def _fetch_json(client: httpx.AsyncClient, base_url: str, path: str, auth: tuple[str, str]) -> dict:
    resp = await client.get(f"{base_url}{path}", auth=auth, timeout=15.0)
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
            dashboard = await _fetch_json(client, base_url, "/admin/dashboard.json", auth)
            persons = await _fetch_json(client, base_url, "/admin/persons.json", auth)
    except httpx.HTTPStatusError as exc:
        raise MetricsFetchError(f"Админка ответила {exc.response.status_code} — проверь ADMIN_USERNAME/ADMIN_PASSWORD") from exc
    except httpx.HTTPError as exc:
        raise MetricsFetchError(f"Не достучался до {base_url} — {exc}") from exc
    return dashboard, persons


def _metric_line(entry: dict) -> str:
    status_icon = {"ok": "✅", "warn": "⚠️", "neutral": "•"}.get(entry.get("status"), "•")
    label = html.escape(str(entry["label"]))
    value = html.escape(str(entry["value"]))
    target = html.escape(str(entry["target"]))
    return f"{status_icon} {label}: {value} (цель {target})"


def build_metrics_digest(dashboard: dict, persons: list[dict], *, today: date | None = None) -> str:
    """Собирает текстовый дайджест для команды из сырых JSON-ответов админки.

    Специально НЕ вызывает LLM (в отличие от sprint-отчёта) — все числа уже
    посчитаны бэкендом (dashboard.json) или считаются тривиальным подсчётом
    списка (persons.json, "новых сегодня"), выдумывать/интерпретировать
    нечего, а ежедневный дайджест не должен зависеть от LLM-бюджета/аптайма.
    """
    today = today or date.today()
    today_str = today.isoformat()

    dq = dashboard.get("data_quality", {})
    product = dashboard.get("product", {})
    au = product.get("active_users", {}).get("latest", {})
    retention = product.get("retention", {})
    paywall = product.get("paywall", {})
    metrics = product.get("metrics", [])

    new_today = sum(1 for p in persons if (p.get("first_seen_at") or "").startswith(today_str))
    active_today = sum(1 for p in persons if p.get("active_24h"))

    lines: list[str] = [f"🐹 Кубыши, привет! Отчёт по метрикам за {today.strftime('%d.%m.%Y')}:", ""]

    lines.append(
        f"Активность: DAU {au.get('dau', '—')} · WAU {au.get('wau', '—')} · MAU {au.get('mau', '—')}"
    )
    lines.append(
        f"Всего людей: {dq.get('known_people', '—')} (+{new_today} новых сегодня, {active_today} активны за 24ч)"
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
            lines.append(f"Retention: {' · '.join(parts)}")
            lines.append("")

    if paywall:
        lines.append(
            "Paywall: показан {shown} раз · триал начали {trial} ({conv}) · покупок {purchases}".format(
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

    return "\n".join(lines).strip()
