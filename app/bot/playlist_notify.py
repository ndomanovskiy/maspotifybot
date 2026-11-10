"""Admin picker: choose who gets told about a new playlist.

The list of people and their ticks live in the message's own keyboard,
so the picker survives a bot restart. _pickers only settles taps that race
each other while the bot is running.
"""

import logging

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.core import edit_text, get_pool, safe_int, send
from app.utils import display_name, escape_html
from app.config import settings

log = logging.getLogger(__name__)

_PREFIX = "pln"
_TICKED, _UNTICKED = "✅", "⬜"

# Newest keyboard of each picker being tapped, or None once it is closed. A tap can
# arrive with an outdated keyboard, and a double tap on Send must not notify twice.
_pickers: dict[tuple[int, int], InlineKeyboardMarkup | None] = {}


def _keyboard(number: int, people: list[tuple[int, str]], selected: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{_TICKED if telegram_id in selected else _UNTICKED} {name}",
            callback_data=f"{_PREFIX}:t:{number}:{telegram_id}",
        )]
        for telegram_id, name in people
    ]
    select_all_label = "Снять всех" if len(selected) == len(people) else "Выбрать всех"
    rows.append([InlineKeyboardButton(text=select_all_label, callback_data=f"{_PREFIX}:all:{number}")])
    rows.append([
        InlineKeyboardButton(text=f"📣 Отправить ({len(selected)})", callback_data=f"{_PREFIX}:send:{number}"),
        InlineKeyboardButton(text="Не отправлять", callback_data=f"{_PREFIX}:skip:{number}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _read_keyboard(markup: InlineKeyboardMarkup) -> tuple[list[tuple[int, str]], set[int]]:
    """Recover the listed people and the ticked ones from the message's keyboard."""
    people, selected = [], set()
    for row in markup.inline_keyboard:
        for button in row:
            parts = (button.callback_data or "").split(":")
            telegram_id = safe_int(parts[3]) if len(parts) > 3 and parts[:2] == [_PREFIX, "t"] else None
            if telegram_id is None:
                continue
            people.append((telegram_id, button.text.partition(" ")[2]))
            if button.text.startswith(_TICKED):
                selected.add(telegram_id)
    return people, selected


async def _load_playlist(number: int):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT name, url FROM playlists WHERE number = $1 ORDER BY id DESC LIMIT 1", number,
        )


async def _quietly(awaitable, what: str) -> None:
    """Await a non-essential Telegram call; its failure must not stop the delivery."""
    try:
        await awaitable
    except Exception as e:
        log.debug(f"{what} skipped: {e}")


async def _deliver(callback: CallbackQuery, playlist, people: list[tuple[int, str]], selected: set[int]) -> None:
    chat_id, message_id = callback.message.chat.id, callback.message.message_id
    name = escape_html(playlist["name"])
    url = escape_html(playlist["url"] or "")
    await _quietly(callback.answer("Отправляю…"), "Button answer")
    await _quietly(edit_text(chat_id, message_id, f"⏳ Отправляю: <b>{name}</b>"), "Progress update")

    delivered, failed = [], []
    for telegram_id, person in people:
        if telegram_id not in selected:
            continue
        try:
            await send(telegram_id, f"🆕 <b>Новый плейлист:</b> {name}\n\nДобавляйте треки!\n{url}")
            delivered.append(person)
        except Exception as e:
            log.warning(f"Failed to notify {telegram_id} about {playlist['name']}: {e}")
            failed.append(person)

    report = [f"📣 Уведомление о <b>{name}</b>"]
    if delivered:
        report.append("✅ Получили: " + ", ".join(escape_html(person) for person in delivered))
    if failed:
        report.append("⚠️ Не дошло: " + ", ".join(escape_html(person) for person in failed))
    text = "\n".join(report)
    try:
        await edit_text(chat_id, message_id, text)
    except Exception as e:
        log.warning(f"Failed to show the delivery report in the picker: {e}")
        await _quietly(send(chat_id, text), "Delivery report message")


async def handle_callback(callback: CallbackQuery) -> None:
    """Handle a tap on any picker button."""
    markup = getattr(callback.message, "reply_markup", None)
    if markup is not None:
        key = (callback.message.chat.id, callback.message.message_id)
        markup = _pickers.get(key, markup)
    if markup is None:
        await callback.answer("Это сообщение уже неактивно")
        return

    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    number = safe_int(parts[2]) if len(parts) > 2 else None
    tapped_id = safe_int(parts[3]) if len(parts) > 3 else None
    if number is None or (action == "t" and tapped_id is None):
        await callback.answer("Ошибка")
        return

    people, selected = _read_keyboard(markup)

    if action == "skip":
        _pickers[key] = None
        await callback.answer()
        await edit_text(callback.message.chat.id, callback.message.message_id, "🔕 Уведомление не отправлено.")
        return

    if action == "send":
        if not selected:
            await callback.answer("Никто не выбран", show_alert=True)
            return
        _pickers[key] = None  # closed before the first await, so a double tap stops above
        try:
            playlist = await _load_playlist(number)
        except Exception as e:
            log.warning(f"Failed to load playlist #{number} for notification: {e}")
            playlist = None
        if playlist is None:
            _pickers[key] = markup
            await callback.answer("Не удалось найти плейлист, попробуй ещё раз", show_alert=True)
            return
        await _deliver(callback, playlist, people, selected)
        return

    if action == "t":
        selected ^= {tapped_id}
    elif action == "all":
        selected = set() if len(selected) == len(people) else {telegram_id for telegram_id, _ in people}

    _pickers[key] = _keyboard(number, people, selected)
    try:
        await callback.message.edit_reply_markup(reply_markup=_pickers[key])
    except Exception as e:
        if "not modified" not in str(e):
            log.warning(f"Failed to redraw picker: {e}")
            _pickers[key] = markup  # the tick never reached the screen
    await _quietly(callback.answer(), "Button answer")


def _person_label(row) -> str:
    if row["telegram_username"] or row["telegram_name"]:
        return display_name(row["telegram_username"], row["telegram_name"])
    return f"id {row['telegram_id']}"


async def offer_notification(playlist: dict) -> None:
    """Send the admin a private picker of who to notify about a just-created playlist."""
    admin_id = settings.telegram_admin_id
    try:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id, telegram_username, telegram_name FROM users WHERE telegram_id <> $1",
                admin_id,
            )
        people = sorted(
            ((r["telegram_id"], _person_label(r)) for r in rows),
            key=lambda person: person[1].lower().lstrip("@"),
        )
        if not people:
            return
        await send(
            admin_id,
            f"🔔 Кому отправить уведомление о <b>{escape_html(playlist['name'])}</b>?",
            reply_markup=_keyboard(playlist["number"], people, set()),
        )
    except Exception as e:
        log.warning(f"Failed to offer playlist notification: {e}")
        try:
            await send(admin_id, "⚠️ Не удалось показать список для уведомления о плейлисте.")
        except Exception as warn_error:
            log.debug(f"Failed to warn admin about the picker: {warn_error}")
