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
    """Generate interesting facts about a track using Claude Sonnet."""
    if not settings.anthropic_api_key:
        return ""

    client = _get_client()
    try:
        response = await client.messages.create(
            model=_RECAP_MODEL,
            max_tokens=700,
            system=(
                "Ты — TURDOM Assistant, музыкальный эксперт. "
                "Напиши ровно 5 самых интересных тезисных фактов о треке. "
                "Выбери лучшие из возможных категорий:\n"
                "• Все артисты трека и их роли (feat, prod, sample)\n"
                "• История создания — как записывали, кто участвовал, backstory\n"
                "• Коммерческий успех — чарты, стриминг, платины, награды\n"
                "• Использование в кино, играх, сериалах, рекламе, мемах, TikTok\n"
                "• Сэмплы и отсылки — что засэмплили, на что ссылаются\n"
                "• Связи артиста — с кем коллабил, откуда, лейбл\n"
                "• Жанровый контекст — почему трек важен для жанра\n"
                "• Живые выступления — культовые перформансы\n"
                "• Скандалы или controversy вокруг трека\n"
                "• Рекорды — первый/последний/единственный в своём роде\n\n"
                "Выбирай самые яркие факты, а не очевидные. "
                "Если про трек мало известно — напиши сколько есть, не выдумывай.\n\n"
                "Формат: каждый факт с новой строки, начинается с эмоджи. "
                "Пиши на русском, тезисно, без воды. "
                "Уложись в 750 символов суммарно. "
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

        # Validate: check if response was cut off (stop_reason != 'end_turn')
        if response.stop_reason != "end_turn":
            log.warning(f"Facts for '{title}' cut off (stop_reason={response.stop_reason}), discarding")
            return ""

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


_RECAP_MODEL = "claude-sonnet-4-6"

_RECAP_BASE_SYSTEM = (
    "Ты — TURDOM Assistant, ведущий музыкальных сессий группы друзей. "
    "Пиши на русском, неформально, с эмоджи. Не повторяй числа и статистику. "
    "НЕ пиши заголовок блока — он уже добавлен. Сразу начинай с содержания. "
    + _HTML_FORMAT_INSTRUCTION
)


async def _generate_recap_block(user_content: str, block_system: str) -> str:
    """Generate a single recap block via Sonnet."""
    if not settings.anthropic_api_key:
        return ""

    client = _get_client()
    try:
        response = await client.messages.create(
            model=_RECAP_MODEL,
            max_tokens=500,
            system=f"{_RECAP_BASE_SYSTEM}\n\n{block_system}",
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Failed to generate recap block: {e}")
        return ""


async def generate_session_recap_blocks(
    total_tracks: int,
    kept: int,
    dropped: int,
    tracks_info: str,
    participants: list[str],
    mimic_info: str,
    rebel_info: str,
    killers_info: str,
) -> dict[str, str]:
    """Generate AI commentary blocks for session recap.

    Each block is generated by a separate Sonnet call for quality.
    Returns dict with keys: genres, transitions, mimic, rebel, facts.
    """
    if not settings.anthropic_api_key:
        return {}

    user_context = (
        f"Участники: {', '.join(participants)}\n"
        f"Всего: {total_tracks}, осталось: {kept}, удалено: {dropped}\n"
        f"{mimic_info}\n{rebel_info}\n{killers_info}\n\n"
        f"Треки (в порядке прослушивания, с жанрами и фактами):\n{tracks_info}"
    )

    block_prompts = {
        "genres": (
            "Напиши комментарий про жанровое разнообразие сессии. "
            "Какие жанры доминировали, что неожиданного было, какие сочетания удивили. "
            "3-4 предложения, живо и с характером."
        ),
        "transitions": (
            "Напиши комментарий про переходы между треками в сессии. "
            "Упомяни каким треком начали и каким закончили (пару слов о каждом). "
            "Если были резкие жанровые скачки (например от металкора к попу) — подчеркни с юмором. "
            "Опиши общую атмосферу сессии. 3-4 предложения."
        ),
        "mimic": (
            "Напиши про Мимика сессии — человека который лучше всех попадает в общий вайб группы. "
            "Его данные указаны в контексте. "
            "Подбери ему смешное подходящее звание в зависимости от атмосферы "
            "(Мимик, Телепат, Вайбмейкер, Хамелеон и т.п. — выбери одно). "
            "Объясни почему он заслужил это звание. 3-4 предложения."
        ),
        "rebel": (
            "Напиши про Бунтаря сессии — человека чьи треки дропали чаще всего. "
            "Его данные и киллеры указаны в контексте. "
            "Он идёт против системы, его вкус не совпадает с группой (сегодня). "
            "Опиши с юмором. Потом напиши кто его Киллеры — те кто голосовал против его треков. "
            "Подай это смешно, типа 'больше всего ударов в спину нанесли...'. "
            "Если киллеров несколько — перечисли всех. 3-4 предложения."
        ),
        "facts": (
            "Найди интересные совпадения и закономерности среди треков сессии. "
            "Примеры: артисты из одной страны, похожие названия треков, "
            "связи между артистами, интересные факты из предоставленных данных, "
            "необычные сочетания жанров. "
            "Подай как забавные наблюдения. 3-4 предложения."
        ),
    }

    # Run all 5 blocks in parallel
    import asyncio
    keys = list(block_prompts.keys())
    results = await asyncio.gather(
        *[_generate_recap_block(user_context, block_prompts[k]) for k in keys],
        return_exceptions=True,
    )

    blocks = {}
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            log.error(f"Recap block {key} failed: {result}")
        elif result:
            blocks[key] = result
    log.info(f"Generated {len(blocks)} recap blocks in parallel")

    return blocks
