import logging

import anthropic

from app.config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def generate_track_facts(title: str, artist: str, album: str) -> str:
    """Generate interesting facts about a track using Claude."""
    if not settings.anthropic_api_key:
        return ""

    client = _get_client()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "Ты — TURDOM Assistant, музыкальный эксперт для группы друзей, которые слушают музыку вместе. "
                "Напиши 2-3 коротких интересных факта о треке. "
                "Факты должны быть увлекательными: история создания, забавные истории, рекорды, связи с другими треками. "
                "Пиши на русском, неформально, коротко. Без заголовков, просто факты через перенос строки. "
                "Используй эмоджи умеренно. "
                "Пиши чистый текст без Markdown разметки (без **, #, _, ` и пр.)."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Трек: {title}\nАртист: {artist}\nАльбом: {album}",
                },
            ],
        )
        facts = response.content[0].text.strip()
        log.info(f"Generated facts for '{title}' by {artist}")
        return facts
    except Exception as e:
        log.error(f"Failed to generate facts: {e}")
        return ""


async def generate_pre_recap_teaser(
    total_tracks: int,
    participants: list[str],
    top_contributor: str | None = None,
) -> str:
    """Generate a teaser at session end — builds intrigue before recap."""
    if not settings.anthropic_api_key:
        return f"🎧 Сегодня {total_tracks} треков. Чем всё закончится? Узнаем прямо сейчас!"

    client = _get_client()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system=(
                "Ты — TURDOM Assistant, ведущий музыкальных сессий. Напиши короткий тизер (2-3 предложения) "
                "который будет показан В КОНЦЕ сессии перед итогами. "
                "Задача: создать интригу. Обыграй количество треков, участников, кто больше всех накидал. "
                "Заверши чем-то типа 'Чем всё закончилось? 🥁' или 'А теперь — итоги!' "
                "Стиль: неформальный, с эмоджи, как шоу-ведущий. "
                "Чистый текст без Markdown разметки."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Треков в плейлисте: {total_tracks}\n"
                        f"Участники: {', '.join(participants)}\n"
                        f"Больше всех треков добавил: {top_contributor or 'неизвестно'}"
                    ),
                },
            ],
        )
        return response.content[0].text.strip()
    except Exception:
        return f"🎧 Сегодня {total_tracks} треков. Чем всё закончится? Узнаем прямо сейчас! 🥁"


async def generate_session_recap(
    total_tracks: int,
    kept: int,
    dropped: int,
    tracks_data: list[dict],
    participants: list[str],
) -> str:
    """Generate an engaging session recap using Claude."""
    if not settings.anthropic_api_key:
        return ""

    kept_tracks = [t for t in tracks_data if t["vote_result"] == "keep"]
    dropped_tracks = [t for t in tracks_data if t["vote_result"] == "drop"]

    tracks_info = ""
    for t in tracks_data:
        status = "✅" if t["vote_result"] == "keep" else "❌"
        tracks_info += f"{status} {t['title']} — {t['artist']} (added by {t.get('added_by', '?')})\n"

    client = _get_client()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "Ты — TURDOM Assistant, ведущий музыкальных сессий. Напиши весёлый рекап сессии. "
                "Включи: общую атмосферу, самый спорный трек, MVP сессии (кто добавил больше сохранённых треков), "
                "шутку или наблюдение. На русском, неформально, с эмоджи. 5-8 предложений. "
                "Треки указаны в порядке прослушивания. "
                "ВАЖНО: пиши чистый текст без форматирования. Никакого Markdown, никаких **, #, _ и прочих символов разметки."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Участники: {', '.join(participants)}\n"
                        f"Всего треков: {total_tracks}, оставлено: {kept}, удалено: {dropped}\n\n"
                        f"Треки:\n{tracks_info}"
                    ),
                },
            ],
        )
        recap = response.content[0].text.strip()
        log.info("Generated AI session recap")
        return recap
    except Exception as e:
        log.error(f"Failed to generate recap: {e}")
        return ""
