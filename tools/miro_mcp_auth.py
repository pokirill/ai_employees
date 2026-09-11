"""One-time OAuth bootstrap for Team Helper -> Miro MCP.

Run this as the dedicated Miro service user, select the team containing the
Kubyshka board, then copy the generated credentials file to the bot server.
Normal bot runs refresh the token non-interactively.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from shared.miro_mcp_client import DEFAULT_REDIRECT_URI, DEFAULT_SERVER_URL, MiroMCPClient


load_dotenv()


def main() -> None:
    auth_file = os.getenv("MIRO_MCP_AUTH_FILE", ".miro_mcp_auth.json").strip()
    client = MiroMCPClient(
        auth_file=auth_file,
        server_url=os.getenv("MIRO_MCP_SERVER_URL", DEFAULT_SERVER_URL).strip(),
        redirect_uri=os.getenv("MIRO_MCP_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
        interactive=True,
    )
    client.authorize()
    path = Path(auth_file).expanduser().resolve()
    print(f"\nMiro MCP авторизован. Credentials: {path}")
    print("Не коммить этот файл в git. На сервере укажи тот же MIRO_MCP_AUTH_FILE.")


if __name__ == "__main__":
    main()
