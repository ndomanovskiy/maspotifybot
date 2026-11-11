"""Ask whoever added a track for its genre when the bot could not find one.

The track and the ticked genres live in the message's own keyboard, so the question
survives a bot restart. _questions only settles taps that race each other while the
bot is running, the same way as in playlist_notify.
"""

import logging
from collections.abc import Awaitable

from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.core import edit_text, get_pool, safe_int, send
from app.services.genre_distributor import PICK_TAGS, classify_track_multi, suppressing_genre
from app.services.genre_resolver import save_genre_if_missing
from app.services.track_formatter import format_track
from app.utils import escape_html

log = logging.getLogger(__name__)

_PREFIX = "gnr"
_TICKED = "✅"
_PER_ROW = 3

# Button label of each tag, in the order of the genre playlists
_LABELS = {tag: name.removeprefix("TURDOM ") for name, tag in PICK_TAGS.items()}
_PLAYLIST_OF = {tag: name for name, tag in PICK_TAGS.items()}

# Newest keyboard of each question being tapped, or None once it is answered. A tap can
# arrive with an outdated keyboard, and a double tap on «Сохранить» must not answer twice.
_questions: dict[tuple[int, int], InlineKeyboardMarkup | None] = {}


def _question_text(track_title: str, artist: str, track_id: str, playlist_name: str) -> str:
    return "\n".join([
        "🎸 <b>Какой жанр у трека?</b>",
        "",
        f"🎵 {format_track(track_title, artist, track_id)}",
        f"в <b>{escape_html(playlist_name)}</b>",
        "",
        "Бот не нашёл жанр сам. Отметь один или несколько и нажми «Сохранить»: "
        "если трек оставят на сессии, по ним его разложат по жанровым плейлистам.",
    ])


def _playlist_label(playlist: str) -> str:
    return escape_html(playlist.removeprefix("TURDOM "))


def _saved_text(picked: list[str]) -> str:
    """Where the picked genres take the track, and why a picked one the hierarchy leaves out is not there."""
    going = classify_track_multi(", ".join(picked))
    reasons = []
    for tag in picked:
        playlist = _PLAYLIST_OF[tag]
        if playlist in going:
            continue
        by = suppressing_genre(playlist, going)
        reason = f"{_playlist_label(playlist)} не добавлен"
        if by:
            reason += f": с {_playlist_label(by)} трек в {_playlist_label(playlist)} не идёт"
        reasons.append(reason)
    text = "✅ Жанр: " + ", ".join(_playlist_label(name) for name in PICK_TAGS if name in going)
    return f"{text} ({'; '.join(reasons)})" if reasons else text


def _keyboard(track_db_id: int, selected: set[str]) -> InlineKeyboardMarkup:
    genres = [
        InlineKeyboardButton(
            text=f"{_TICKED} {label}" if tag in selected else label,
            callback_data=f"{_PREFIX}:t:{track_db_id}:{tag}",
        )
        for tag, label in _LABELS.items()
    ]
    rows = [genres[i:i + _PER_ROW] for i in range(0, len(genres), _PER_ROW)]
    rows.append([
        InlineKeyboardButton(text="💾 Сохранить", callback_data=f"{_PREFIX}:save:{track_db_id}"),
        InlineKeyboardButton(text="🤷 Не знаю", callback_data=f"{_PREFIX}:skip:{track_db_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _read_keyboard(markup: InlineKeyboardMarkup) -> tuple[int | None, set[str]]:
    """Recover the track and the ticked genres from the question's keyboard."""
    track_db_id, selected = None, set()
    for row in markup.inline_keyboard:
        for button in row:
            parts = (button.callback_data or "").split(":", 3)
            if len(parts) != 4 or parts[:2] != [_PREFIX, "t"] or parts[3] not in _LABELS:
                continue
            track_db_id = safe_int(parts[2])
            if button.text.startswith(_TICKED):
                selected.add(parts[3])
    return track_db_id, selected


async def ask_genre(*, telegram_id: int, track_db_id: int, track_title: str, artist: str,
                    track_id: str, playlist_name: str) -> None:
    """Send whoever added the track the question about its genre."""
    try:
        await send(
            telegram_id,
            _question_text(track_title, artist, track_id, playlist_name),
            reply_markup=_keyboard(track_db_id, set()),
        )
    except Exception as e:
        log.warning(f"Failed to ask {telegram_id} about the genre of '{track_title}': {e}")


async def _quietly(awaitable: Awaitable[object], what: str) -> None:
    """Await a non-essential Telegram call; its failure must not stop the rest."""
    try:
        await awaitable
    except Exception as e:
        log.debug(f"{what} skipped: {e}")


async def _close(callback: CallbackQuery, outcome: str) -> None:
    """Put the outcome under the question in place of its buttons."""
    message = callback.message
    try:
        await edit_text(message.chat.id, message.message_id, f"{message.html_text}\n\n{outcome}")
    except Exception as e:
        log.warning(f"Failed to close the genre question: {e}")


async def handle_callback(callback: CallbackQuery) -> None:
    """Handle a tap on any button of a genre question."""
    message = callback.message
    if isinstance(message, InaccessibleMessage):
        # Telegram stops showing the bot an old message, and with it the question's keyboard
        log.info(f"Genre question {message.message_id} in {message.chat.id} is too old to read")
        await _quietly(callback.answer("Вопрос устарел: бот больше не видит это сообщение", show_alert=True), "Button answer")
        return
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        await _quietly(callback.answer("Этот вопрос уже закрыт"), "Button answer")
        return
    key = (message.chat.id, message.message_id)
    markup = _questions.get(key, markup)
    # The track comes from the question itself, so a tap can only answer the question it was on
    track_db_id, selected = _read_keyboard(markup) if markup is not None else (None, set())
    if track_db_id is None:
        await _quietly(callback.answer("Этот вопрос уже закрыт"), "Button answer")
        return

    parts = callback.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""

    if action == "t":
        tag = parts[3] if len(parts) > 3 else ""
        if tag not in _LABELS:
            await _quietly(callback.answer("Ошибка"), "Button answer")
            return
        redrawn = _keyboard(track_db_id, selected ^ {tag})
        _questions[key] = redrawn
        try:
            await message.edit_reply_markup(reply_markup=redrawn)
        except Exception as e:
            if "not modified" not in str(e):
                log.warning(f"Failed to redraw the genre question: {e}")
                if _questions.get(key) is redrawn:  # no later tap has moved on from this tick
                    _questions[key] = markup  # the tick never reached the screen
                    await _quietly(callback.answer("Не получилось отметить, нажми ещё раз"), "Button answer")
                    return
        await _quietly(callback.answer(), "Button answer")
        return

    if action == "skip":
        _questions[key] = None
        await _quietly(callback.answer(), "Button answer")
        await _close(callback, "🤷 Понял, трек останется без жанра.")
        return

    if action == "save":
        if not selected:
            await _quietly(
                callback.answer("Отметь хотя бы один жанр или нажми «Не знаю»", show_alert=True), "Button answer",
            )
            return
        _questions[key] = None  # closed before the first await, so a double tap stops above
        picked = [tag for tag in _LABELS if tag in selected]
        try:
            saved = await save_genre_if_missing(get_pool(), track_db_id, ", ".join(picked))
        except Exception as e:
            log.warning(f"Failed to save the genre of track {track_db_id}: {e}")
            _questions[key] = markup
            await _quietly(callback.answer("Не удалось сохранить, попробуй ещё раз", show_alert=True), "Button answer")
            return
        await _quietly(callback.answer("Сохранено" if saved else "Жанр уже указан"), "Button answer")
        await _close(callback, _saved_text(picked) if saved else "ℹ️ Жанр уже указан, ничего не менял.")
        return

    await _quietly(callback.answer("Ошибка"), "Button answer")
