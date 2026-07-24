from __future__ import annotations

import httpx

_NEXARA_URL = "https://api.nexara.ru/v1/audio/transcriptions"


class TranscriptionClient:
    """Транскрибация записей встреч через Nexara (api.nexara.ru) — не то же
    самое, что LLMClient.transcribe() (OpenAI Whisper): Nexara умеет
    диаризацию (task=diarize, roles=auto — размечает реплики по спикерам) и
    принимает файлы до 3 ГБ вместо лимита OpenAI в 25 МБ, что для записи
    встречи в 30-60 минут может быть решающим."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def transcribe(self, file_path: str, *, language: str = "ru") -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {"task": "diarize", "roles": "auto", "response_format": "verbose_json", "language": language}
        async with httpx.AsyncClient(timeout=600.0) as client:
            with open(file_path, "rb") as audio_file:
                response = await client.post(
                    _NEXARA_URL, headers=headers, data=data, files={"file": audio_file}
                )
        response.raise_for_status()
        payload = response.json()
        segments = payload.get("segments")
        if not segments:
            return payload.get("text", "")
        lines = []
        for segment in segments:
            start = segment.get("start", 0.0)
            minutes, seconds = divmod(int(start), 60)
            speaker = segment.get("speaker", "?")
            lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {segment.get('text', '').strip()}")
        return "\n".join(lines)
