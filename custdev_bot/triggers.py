from __future__ import annotations

# R-CONVENIENCE: бот вступает в разговор РЕАКТИВНО — только когда тема
# отслеживания денег/бюджета уже всплыла сама, не заводит разговор с нуля.
# Так выглядит естественнее и меньше похоже на спам (см. Kirill's ask:
# "спрашивать в чатах как/почему люди следят за деньгами").
_TRIGGER_KEYWORDS = (
    "бюджет",
    "трачу",
    "траты",
    "накопить",
    "накопления",
    "эконом",
    "куда уходят деньги",
    "финанс",
    "не хватает денег",
    "веду учёт",
    "учёт расходов",
    "откладыва",
    "зарплат",
)


def mentions_money_tracking(text: str) -> bool:
    normalized = text.lower()
    return any(keyword in normalized for keyword in _TRIGGER_KEYWORDS)
