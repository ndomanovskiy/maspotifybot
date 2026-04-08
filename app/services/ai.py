import logging

import anthropic

from app.config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None

_HTML_FORMAT_INSTRUCTION = (
    "Форматируй текст в Telegram HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>. "
    "НЕ используй Markdown (**, #, _, `). Только HTML теги."
)


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
                + _HTML_FORMAT_INSTRUCTION
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
                + _HTML_FORMAT_INSTRUCTION
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
    tracks_info: str,
    participants: list[str],
    mimic_info: str,
    rebel_info: str,
    killers_info: str,
) -> str:
    """Generate AI commentary for session recap."""
    if not settings.anthropic_api_key:
        return ""

    client = _get_client()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=(
                "Ты — TURDOM Assistant, ведущий музыкальных сессий группы друзей. "
                "Напиши комментарий к рекапу сессии. Структура:\n\n"
                "1. Каким треком начали, каким закончили — по паре слов о каждом. "
                "Какая была атмосфера сессии в целом.\n"
                "2. Если были резкие жанровые переходы (например от металкора к попу) — "
                "подчеркни это с юмором.\n"
                "3. Мимик сессии — человек который лучше всех попадает в общий вайб группы. "
                "Подбери ему подходящее смешное звание в зависимости от атмосферы сессии "
                "(например 'Мимик', 'Телепат', 'Вайбмейкер' и т.п.). Распиши коротко.\n"
                "4. Бунтарь — человек чьи треки дропали чаще всего. Он идёт против системы. "
                "Опиши с юмором. Потом напиши кто его 'Киллеры' — те кто голосовал против его треков. "
                "Подай это смешно, типа 'больше всего ударов в спину нанесли...'.\n"
                "5. Если в треках есть интересные совпадения (артисты из одной страны, "
                "похожие названия, связи между треками из фактов) — отметь.\n\n"
                "Пиши на русском, неформально, с эмоджи. Не повторяй числа и статистику — "
                "они уже показаны выше. Просто комментарий от ведущего.\n"
                + _HTML_FORMAT_INSTRUCTION
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Участники: {', '.join(participants)}\n"
                        f"Всего: {total_tracks}, осталось: {kept}, удалено: {dropped}\n"
                        f"{mimic_info}\n{rebel_info}\n{killers_info}\n\n"
                        f"Треки (в порядке прослушивания, с жанрами и фактами):\n{tracks_info}"
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
