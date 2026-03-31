import logging
from openai import AsyncOpenAI

from app.config import settings

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def generate_track_facts(title: str, artist: str, album: str) -> str:
    """Generate interesting facts about a track using GPT-4o-mini."""
    if not settings.openai_api_key:
        return ""

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — музыкальный эксперт для группы друзей TURDOM, которые слушают музыку вместе. "
                        "Напиши 2-3 коротких интересных факта о треке. "
                        "Факты должны быть увлекательными: история создания, забавные истории, рекорды, связи с другими треками. "
                        "Пиши на русском, неформально, коротко. Без заголовков, просто факты через перенос строки. "
                        "Используй эмоджи умеренно."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Трек: {title}\nАртист: {artist}\nАльбом: {album}",
                },
            ],
            max_tokens=300,
            temperature=0.8,
        )
        facts = response.choices[0].message.content.strip()
        log.info(f"Generated facts for '{title}' by {artist}")
        return facts
    except Exception as e:
        log.error(f"Failed to generate facts: {e}")
        return ""


async def generate_session_recap(
    total_tracks: int,
    kept: int,
    dropped: int,
    tracks_data: list[dict],
    participants: list[str],
) -> str:
    """Generate an engaging session recap using GPT-4o-mini."""
    if not settings.openai_api_key:
        return ""

    # Build context about the session
    kept_tracks = [t for t in tracks_data if t["vote_result"] == "keep"]
    dropped_tracks = [t for t in tracks_data if t["vote_result"] == "drop"]

    tracks_info = ""
    for t in tracks_data:
        status = "✅" if t["vote_result"] == "keep" else "❌"
        tracks_info += f"{status} {t['title']} — {t['artist']} (added by {t.get('added_by', '?')})\n"

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — ведущий музыкальных сессий TURDOM. Напиши весёлый рекап сессии. "
                        "Включи: общую атмосферу, самый спорный трек, MVP сессии (кто добавил больше сохранённых треков), "
                        "шутку или наблюдение. На русском, неформально, с эмоджи. 5-8 предложений."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Участники: {', '.join(participants)}\n"
                        f"Всего треков: {total_tracks}, оставлено: {kept}, удалено: {dropped}\n\n"
                        f"Треки:\n{tracks_info}"
                    ),
                },
            ],
            max_tokens=400,
            temperature=0.9,
        )
        recap = response.choices[0].message.content.strip()
        log.info("Generated AI session recap")
        return recap
    except Exception as e:
        log.error(f"Failed to generate recap: {e}")
        return ""
