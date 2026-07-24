from __future__ import annotations

from dataclasses import dataclass

import httpx

_NEXARA_URL = "https://api.nexara.ru/v1/audio/transcriptions"


@dataclass
class MeetingTranscript:
    """Единый формат для обоих движков транскрибации (Nexara и OpenAI Whisper
    в LLMClient.transcribe) — team_bot/main.py работает с одним и тем же
    типом независимо от того, какой из них реально сработал. duration_seconds/
    speaker_count — None у Whisper-фолбэка (там нет диаризации и duration не
    запрашивается — не стоит того усложнения ради редкого запасного пути)."""

    text: str
    duration_seconds: float | None = None
    speaker_count: int | None = None


class TranscriptionClient:
    """Транскрибация записей встреч через Nexara (api.nexara.ru) — не то же
    самое, что LLMClient.transcribe() (OpenAI Whisper): Nexara умеет
    диаризацию (task=diarize, roles=auto — размечает реплики по спикерам) и
    принимает файлы до 3 ГБ вместо лимита OpenAI в 25 МБ, что для записи
    встречи в 30-60 минут может быть решающим."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def transcribe(self, file_path: str, *, language: str = "ru") -> MeetingTranscript:
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
            return MeetingTranscript(text=payload.get("text", ""), duration_seconds=payload.get("duration"))

        lines = []
        speakers = set()
        last_end = 0.0
        for segment in segments:
            start = segment.get("start", 0.0)
            last_end = max(last_end, segment.get("end", start))
            speaker = segment.get("speaker", "?")
            speakers.add(speaker)
            minutes, seconds = divmod(int(start), 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {segment.get('text', '').strip()}")

        return MeetingTranscript(
            text="\n".join(lines),
            duration_seconds=payload.get("duration", last_end),
            speaker_count=len(speakers),
        )
