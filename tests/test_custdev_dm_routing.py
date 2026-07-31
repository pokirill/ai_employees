from __future__ import annotations

import asyncio
import os

os.environ.setdefault("TEAM_BOT_TOKEN", "123456:fake-token-for-tests")
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-tests")

import pytest

import team_bot.main as main


class _FakeMessage:
    """Duck-typed stand-in for aiogram's Message — only what these helpers touch."""

    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


@pytest.fixture(autouse=True)
def _clean_custdev_state():
    # Эти три dict — module-level и живут между тестами, если не чистить.
    main._custdev_sessions.clear()
    main._pending_custdev_invites.clear()
    main._known_private_chats.clear()
    yield
    main._custdev_sessions.clear()
    main._pending_custdev_invites.clear()
    main._known_private_chats.clear()


def test_find_custdev_session_chat_matches_case_insensitively():
    main._custdev_sessions[555] = main.CustDevSession(chat_id=555, admin_username="a", target_username="Ivan")

    assert main._find_custdev_session_chat("ivan") == 555
    assert main._find_custdev_session_chat("IVAN") == 555
    assert main._find_custdev_session_chat("nobody") is None


def test_send_custdev_invite_link_registers_pending_invite_and_replies_with_link():
    main._bot_username = "kubyshka_team_bot"
    message = _FakeMessage()

    asyncio.run(main._send_custdev_invite_link(message, admin_username="fullbyte9", target="ivan"))

    assert len(main._pending_custdev_invites) == 1
    token, (admin, target) = next(iter(main._pending_custdev_invites.items()))
    assert admin == "fullbyte9"
    assert target == "ivan"
    assert len(message.answers) == 1
    assert f"https://t.me/kubyshka_team_bot?start=custdev_{token}" in message.answers[0]
    assert "@ivan" in message.answers[0]


def test_send_custdev_invite_link_each_call_gets_a_fresh_token():
    main._bot_username = "kubyshka_team_bot"

    asyncio.run(main._send_custdev_invite_link(_FakeMessage(), admin_username="a", target="ivan"))
    asyncio.run(main._send_custdev_invite_link(_FakeMessage(), admin_username="a", target="ivan"))

    # Два независимых приглашения одному и тому же human — не должны схлопнуться
    # в один токен (иначе второй _start_custdev_via_deeplink съел бы первый invite).
    assert len(main._pending_custdev_invites) == 2
