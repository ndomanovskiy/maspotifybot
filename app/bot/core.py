"""Shared bot infrastructure: bot instance, dispatcher, helpers, decorators."""

import re
import logging
from functools import wraps

from aiogram import Bot, Dispatcher
from aiogram.types import LinkPreviewOptions, Message, CallbackQuery

from app.config import settings

log = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Pool is set in setup_bot
_pool = None


def set_pool(p):
    global _pool
    _pool = p


def get_pool():
    """Get the database pool. Must be called after set_pool()."""
    return _pool


def is_admin(telegram_id: int) -> bool:
    return telegram_id == settings.telegram_admin_id


async def is_registered(telegram_id: int) -> bool:
    """Check if user is registered in DB."""
    if is_admin(telegram_id):
        return True
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id = $1)",
            telegram_id,
        )


def extract_spotify_id(url_or_id: str, entity: str = "track") -> str | None:
    """Extract Spotify ID from URL or raw ID.

    entity: 'track', 'playlist', or 'user'
    """
    match = re.search(rf"{entity}[/:]([a-zA-Z0-9._-]+)", url_or_id)
    if match:
        return match.group(1)
    if entity == "user":
        if re.match(r"^[a-zA-Z0-9._-]+$", url_or_id) and "/" not in url_or_id:
            return url_or_id
    else:
        if re.match(r"^[a-zA-Z0-9]{22}$", url_or_id):
            return url_or_id
    return None


def safe_int(value: str) -> int | None:
    """Safely parse int from callback data. Returns None on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_turdom_number(text: str) -> int | None:
    """Extract TURDOM number from command arguments like '/distribute 91'."""
    args = text.split(maxsplit=1)
    if len(args) < 2:
        return None
    try:
        return int(args[1].strip())
    except ValueError:
        return None


# --- Access control decorators ---

def require_admin(handler):
    """Decorator: only allow admin to use this command."""
    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not is_admin(message.from_user.id):
            await message.answer("Только админ.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper


def require_registered(handler):
    """Decorator: only allow registered users."""
    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not await is_registered(message.from_user.id):
            await message.answer("⛔ Доступ только для участников TURDOM.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper


# --- Message helpers ---

async def send(chat_id: int, text: str, *, preview: bool = False, **kwargs):
    """Send HTML message. No link preview by default."""
    opts = {} if preview else {"link_preview_options": _NO_PREVIEW}
    return await bot.send_message(chat_id, text, parse_mode="HTML", **opts, **kwargs)


async def reply(message: Message, text: str, *, preview: bool = False, **kwargs):
    """Reply to a message with HTML. No link preview by default."""
    opts = {} if preview else {"link_preview_options": _NO_PREVIEW}
    return await message.answer(text, parse_mode="HTML", **opts, **kwargs)


async def send_photo(chat_id: int, photo: str, caption: str, **kwargs):
    """Send photo with HTML caption."""
    return await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode="HTML", **kwargs)


async def reply_photo(message: Message, photo: str, caption: str, **kwargs):
    """Reply with photo and HTML caption."""
    return await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", **kwargs)


async def edit_text(chat_id: int, message_id: int, text: str, *, preview: bool = False, **kwargs):
    """Edit message text with HTML. No link preview by default."""
    opts = {} if preview else {"link_preview_options": _NO_PREVIEW}
    return await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", **opts, **kwargs)


async def edit_caption(chat_id: int, message_id: int, caption: str, **kwargs):
    """Edit message caption with HTML."""
    return await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption, parse_mode="HTML", **kwargs)


def require_admin_callback(handler):
    """Decorator: only allow admin for callback queries."""
    @wraps(handler)
    async def wrapper(callback: CallbackQuery, *args, **kwargs):
        if not is_admin(callback.from_user.id):
            await callback.answer("Только ведущий!")
            return
        return await handler(callback, *args, **kwargs)
    return wrapper
