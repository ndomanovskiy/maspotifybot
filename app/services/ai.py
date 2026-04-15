import logging

import anthropic
from openai import AsyncOpenAI

from app.config import settings

log = logging.getLogger(__name__)

_anthropic_client: anthropic.AsyncAnthropic | None = None
_openai_client: AsyncOpenAI | None = None

_HTML_FORMAT_INSTRUCTION = (
    "Форматируй текст в Telegram HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>. "
    "НЕ используй Markdown (**, #, _, `). Только HTML теги."
)

_OPENAI_FALLBACK_MODEL = "gpt-4o"


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def get_openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def _call_llm(system: str, user_content: str, max_tokens: int) -> tuple[str, str]:
    """Call Anthropic Sonnet, fallback to GPT-4o on overload.

    Returns (text, stop_reason). stop_reason is 'end_turn' for complete responses.
    """
    # Try Anthropic first
    if settings.anthropic_api_key:
        try:
            client = _get_anthropic()
            response = await client.messages.create(
                model=_RECAP_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            return response.content[0].text.strip(), response.stop_reason
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                log.warning(f"Anthropic overloaded (529), skipping")
                # TODO: uncomment to enable GPT-4o fallback
                # if settings.openai_api_key:
                #     return await _call_openai_fallback(system, user_content, max_tokens)
                return "", "error"
            else:
                raise

    return "", "error"


async def generate_track_facts(title: str, artist: str, album: str, release_date: str = "") -> str:
    """Generate interesting facts about a track. Sonnet → GPT-4o fallback."""
    if not settings.anthropic_api_key and not settings.openai_api_key:
        return ""

    date_hint = ""
    if release_date:
        date_hint = (
            f"Дата релиза альбома: {release_date}. "
            "Первый факт — 📅 год выхода оригинала. Если это ремастер — укажи оба года: оригинал и ремастер. "
        )

    system = (
        "Ты — TURDOM Assistant, музыкальный эксперт. "
        "Напиши ровно 5 самых интересных тезисных фактов о треке. "
        f"{date_hint}"
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
        "Если трек тебе неизвестен — напиши 2-3 строки про жанр, настроение и стиль артиста, "
        "и в конце добавь строку: '🔍 <i>Артист пока мало известен — фактов в открытых источниках немного</i>'. "
        "НЕ извиняйся, НЕ проси ссылки. Просто опиши вайб + пометка.\n\n"
        "Формат: каждый факт с новой строки, начинается с эмоджи. "
        "Пиши на русском, тезисно, без воды. "
        "Уложись в 750 символов суммарно. "
        + _HTML_FORMAT_INSTRUCTION
    )

    try:
        facts, stop_reason = await _call_llm(system, f"Трек: {title}\nАртист: {artist}\nАльбом: {album}", 700)

        if stop_reason != "end_turn":
            log.warning(f"Facts for '{title}' cut off (stop_reason={stop_reason}), discarding")
            return ""

        # Filter out cop-out lines where model admits it doesn't know
        _copout_markers = ["помогу", "дайте знать", "поделитесь", "не удалось найти",
                           "недостаточно информации", "не нашёл", "попытаться помочь",
                           "к сожалению", "публично не задокументирована",
                           "информация ограничена", "данных недостаточно"]
        lines = facts.split("\n")
        clean_lines = [l for l in lines if not any(m in l.lower() for m in _copout_markers)]
        facts = "\n".join(clean_lines).strip()

        if not facts:
            log.warning(f"Facts for '{title}' all cop-out, discarding")
            return ""

        log.info(f"Generated facts for '{title}' by {artist}")
        return facts
    except Exception as e:
        log.error(f"Failed to generate facts: {e}")
        return ""


async def analyze_easter_egg(secret: str, tracks: list[dict]) -> str:
    """Analyze user's easter egg against their tracks. Returns AI response."""
    tracks_list = "\n".join(f"• {t['title']} — {t['artist']}" for t in tracks)

    system = (
        "Ты — TURDOM Assistant. Пользователь оставил пасхалку (секрет) про свои треки в плейлисте. "
        "Попробуй сопоставить пасхалку с треками.\n\n"
        "ВАЖНО: ищи совпадения и по НАЗВАНИЮ ТРЕКА и по ИМЕНИ АРТИСТА. "
        "Например если пасхалка упоминает 'Oingo Boingo' — ищи артиста 'Oingo Boingo' в списке. "
        "Если пасхалка упоминает 'King Crimson' — ищи артиста 'King Crimson'. "
        "Пользователь может неточно называть треки (из голосового ввода), "
        "сопоставляй по смыслу, не требуй точного совпадения.\n\n"
        "Для каждого совпавшего трека напиши: название трека, артист, и как он связан с пасхалкой.\n"
        "Если не уверен или понял только частично — спроси уточнение.\n"
        "Если совсем не понял — честно скажи и попроси подсказку.\n\n"
        "На русском, неформально. "
        + _HTML_FORMAT_INSTRUCTION
    )

    try:
        text, _ = await _call_llm(
            system,
            f"Пасхалка: {secret}\n\nТреки пользователя (формат: Название — Артист):\n{tracks_list}",
            1000,
        )
        return text
    except Exception as e:
        log.error(f"Failed to analyze easter egg: {e}")
        return ""


async def generate_pre_recap_teaser(
    total_tracks: int,
    participants: list[str],
    top_contributor: str | None = None,
) -> str:
    """Generate a teaser at session end — builds intrigue before recap."""
    if not settings.anthropic_api_key and not settings.openai_api_key:
        return f"🎧 Сегодня {total_tracks} треков. Чем всё закончится? Узнаем прямо сейчас!"

    system = (
        "Ты — TURDOM Assistant, ведущий музыкальных сессий. Напиши короткий тизер (2-3 предложения) "
        "который будет показан В КОНЦЕ сессии перед итогами. "
        "Задача: создать интригу. Обыграй количество треков, участников, кто больше всех накидал. "
        "Заверши чем-то типа 'Чем всё закончилось? 🥁' или 'А теперь — итоги!' "
        "Стиль: неформальный, с эмоджи, как шоу-ведущий. "
        + _HTML_FORMAT_INSTRUCTION
    )
    try:
        text, _ = await _call_llm(
            system,
            f"Треков в плейлисте: {total_tracks}\n"
            f"Участники: {', '.join(participants)}\n"
            f"Больше всех треков добавил: {top_contributor or 'неизвестно'}",
            120,
        )
        return text or f"🎧 Сегодня {total_tracks} треков. Чем всё закончится? Узнаем прямо сейчас! 🥁"
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
    if not settings.anthropic_api_key and not settings.openai_api_key:
        return ""

    try:
        text, _ = await _call_llm(f"{_RECAP_BASE_SYSTEM}\n\n{block_system}", user_content, 500)
        return text
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
    if not settings.anthropic_api_key and not settings.openai_api_key:
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

    # Run all blocks in parallel
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
