from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86_400) -> dict:
    """Проверяет подпись Telegram Mini App initData (см.
    core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    Без этой проверки любой мог бы дёргать API мини-аппа от чужого имени —
    initData подписана HMAC на секрете, производном от токена бота, так что
    подделать её без знания токена нельзя. Возвращает распарсенные поля
    (включая "user" как dict), иначе бросает InvalidInitData."""
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("initData без hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("Подпись initData не совпадает — запрос не от Telegram")

    auth_date = data.get("auth_date")
    if auth_date and time.time() - int(auth_date) > max_age_seconds:
        raise InvalidInitData("initData устарела, открой мини-апп заново")

    if "user" in data:
        data["user"] = json.loads(data["user"])
    return data
