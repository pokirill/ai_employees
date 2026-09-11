"""Minimal production client for Miro's remote MCP server.

The team bot is a daemon, while Miro MCP uses OAuth 2.1 with dynamic client
registration. Authorization therefore happens once from a terminal and the
resulting client registration + refresh token are persisted in a small JSON
file. Normal bot runs only refresh tokens; they never try to open a browser.

One important Miro limitation: an MCP authorization is tied to a Miro *team*
and Miro currently keeps a 1:1 MCP connection per user. Use a dedicated Miro
service user for Team Helper, otherwise authorizing the daemon may invalidate a
human's ChatGPT/Claude/Cursor Miro connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_SERVER_URL = "https://mcp.miro.com/"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"


class MiroMCPError(RuntimeError):
    pass


class MiroMCPAuthRequired(MiroMCPError):
    pass


class JsonOAuthStorage:
    """Persistent TokenStorage implementation for the MCP Python SDK.

    The SDK needs to keep both the dynamically registered client information
    and OAuth tokens between runs. Writes are atomic so a bot restart cannot
    leave a half-written credentials file.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MiroMCPError(f"Не удалось прочитать OAuth-файл Miro {self.path}: {exc}") from exc

    def _save_part(self, key: str, value: Any) -> None:
        with self._lock:
            payload: dict[str, Any]
            if self.path.exists():
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = {}
            else:
                payload = {}
            payload[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        value = self._load().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens) -> None:
        self._save_part("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        value = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info) -> None:
        self._save_part("client_info", client_info.model_dump(mode="json", exclude_none=True))


class MiroMCPClient:
    """Small synchronous facade over the async remote MCP client.

    Team Helper calls network integrations from a worker thread, so a sync
    facade keeps the rest of the task sync engine simple. Each call opens one
    short MCP session; the expensive OAuth login is not repeated because token
    and client information live in :class:`JsonOAuthStorage`.
    """

    def __init__(
        self,
        *,
        auth_file: str,
        server_url: str = DEFAULT_SERVER_URL,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        interactive: bool = False,
    ) -> None:
        self.server_url = (server_url or DEFAULT_SERVER_URL).strip()
        self.redirect_uri = (redirect_uri or DEFAULT_REDIRECT_URI).strip()
        self.storage = JsonOAuthStorage(auth_file)
        self.interactive = interactive

    @property
    def authorized(self) -> bool:
        return self.storage.exists()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.interactive and not self.storage.exists():
            raise MiroMCPAuthRequired(
                "Miro MCP не авторизован. Один раз запусти "
                "`python tools/miro_mcp_auth.py` от отдельного Miro-пользователя, "
                "после чего положи созданный OAuth-файл на сервер."
            )
        return _run_coroutine(self._call_tool(name, arguments))

    def authorize(self) -> None:
        """Run one interactive OAuth flow and persist credentials."""
        if not self.interactive:
            raise MiroMCPError("authorize() доступен только в interactive-режиме")
        _run_coroutine(self._authorize())

    async def _authorize(self) -> None:
        # Listing tools is enough to force the OAuth handshake.
        await self._call_tool("__list_tools__", {})

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx2
            from mcp import Client
            from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.auth import OAuthClientMetadata
            from pydantic import AnyUrl
        except ImportError as exc:
            raise MiroMCPError(
                "Не установлен MCP Python SDK. Выполни `pip install -r requirements.txt`."
            ) from exc

        async def redirect_handler(authorization_url: str) -> None:
            if not self.interactive:
                raise MiroMCPAuthRequired(
                    "OAuth Miro требует повторной авторизации. Запусти "
                    "`python tools/miro_mcp_auth.py` и заново выбери команду Miro."
                )
            print("\nОткрой Miro и подтверди доступ Team Helper:\n")
            print(authorization_url)
            print()
            try:
                webbrowser.open(authorization_url)
            except Exception:
                pass

        async def callback_handler():
            if not self.interactive:
                raise MiroMCPAuthRequired("Miro MCP запросил интерактивный OAuth callback")
            redirected = await asyncio.to_thread(
                input,
                "После авторизации скопируй ПОЛНЫЙ URL из адресной строки браузера и вставь сюда:\n> ",
            )
            parsed = parse_qs(urlparse(redirected.strip()).query)
            if "code" not in parsed or "state" not in parsed:
                raise MiroMCPError("В callback URL нет code/state — авторизация не завершена")
            return AuthorizationCodeResult(
                code=parsed["code"][0],
                state=parsed["state"][0],
                iss=parsed.get("iss", [None])[0],
            )

        oauth = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name="Kubyshka Team Helper",
                redirect_uris=[AnyUrl(self.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                # Miro advertises the concrete scopes during discovery; leaving
                # scope=None lets the provider use the server's default set.
                scope=None,
            ),
            storage=self.storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

        try:
            async with httpx2.AsyncClient(auth=oauth, follow_redirects=True) as http_client:
                transport = streamable_http_client(self.server_url, http_client=http_client)
                async with Client(transport) as client:
                    if name == "__list_tools__":
                        tools = await client.list_tools()
                        return {"tools": [tool.name for tool in tools.tools]}
                    result = await client.call_tool(name, arguments)
        except MiroMCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport/auth errors vary by SDK version
            text = str(exc)
            if "401" in text or "authorization" in text.lower() or "oauth" in text.lower():
                raise MiroMCPAuthRequired(
                    "Miro MCP отклонил OAuth-сессию. Запусти `python tools/miro_mcp_auth.py` повторно."
                ) from exc
            raise MiroMCPError(f"Miro MCP недоступен: {exc}") from exc

        if getattr(result, "is_error", False):
            message = _text_from_result(result) or "неизвестная ошибка MCP tool"
            raise MiroMCPError(f"Miro MCP {name}: {message[:500]}")

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured

        text = _text_from_result(result)
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        return value if isinstance(value, dict) else {"data": value}


def _text_from_result(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _run_coroutine(coro):
    """Run an async MCP call from synchronous sync code.

    In production this runs inside ``asyncio.to_thread`` and therefore has no
    event loop. The defensive thread fallback also makes direct calls safe in
    tests/tools that happen to already run inside an event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")
