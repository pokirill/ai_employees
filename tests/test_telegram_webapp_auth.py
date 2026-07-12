from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from shared.telegram_webapp_auth import InvalidInitData, validate_init_data

_BOT_TOKEN = "123456:test-token"


def _build_init_data(fields: dict) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", _BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": computed_hash})


def test_valid_init_data_is_accepted_and_parsed():
    init_data = _build_init_data({"auth_date": "2000000000", "user": '{"id": 1, "first_name": "Alex"}'})
    result = validate_init_data(init_data, _BOT_TOKEN, max_age_seconds=10**12)
    assert result["user"]["first_name"] == "Alex"


def test_tampered_field_is_rejected():
    init_data = _build_init_data({"auth_date": "2000000000", "user": '{"id": 1}'})
    tampered = init_data.replace("auth_date=2000000000", "auth_date=1111111111")
    with pytest.raises(InvalidInitData):
        validate_init_data(tampered, _BOT_TOKEN, max_age_seconds=10**12)


def test_missing_hash_is_rejected():
    with pytest.raises(InvalidInitData):
        validate_init_data("auth_date=123&user=%7B%7D", _BOT_TOKEN)


def test_expired_init_data_is_rejected():
    init_data = _build_init_data({"auth_date": "1"})
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data, _BOT_TOKEN, max_age_seconds=86_400)


def test_wrong_bot_token_is_rejected():
    init_data = _build_init_data({"auth_date": "2000000000"})
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data, "different-token", max_age_seconds=10**12)
