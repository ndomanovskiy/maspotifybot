"""Bot package — wires routers and provides setup_bot entry point."""

import asyncio
import logging

from aiogram.types import Message, BotCommand, BotCommandScopeChat

from app.config import settings
from app.spotify.auth import load_token_from_db
from app.services.duplicate_watcher import DuplicateWatcher

from app.bot.core import bot, dp, set_pool, is_admin, send, reply
from app.bot.duplicate_messages import (
    auto_removed_text, dropped_before_text, possible_duplicate_text, sibling_text,
)
from app.bot.playlist_notify import offer_notification
from app.bot.session_manager import session
from app.bot.commands.user import router as user_router
from app.bot.commands.admin import router as admin_router, set_duplicate_notify, set_fuzzy_confirm, init_health
from app.bot.callbacks import router as callbacks_router, keep_duplicate_button, remove_duplicate_button
from app.utils import escape_html

log = logging.getLogger(__name__)

# Register routers
dp.include_router(user_router)
dp.include_router(admin_router)
dp.include_router(callbacks_router)


# Theme input handler (needs lambda filter referencing session state)
@dp.message(lambda m: m.text and not m.text.startswith("/") and session.waiting_theme and is_admin(m.from_user.id))
async def on_theme_input(message: Message):
    if not session.waiting_theme:
        return
    session.waiting_theme = False

    from app.services.playlists import create_next_playlist
    from app.services.admin_commands import log_action
    from app.bot.core import get_pool

    theme = message.text.strip()
    try:
        result = await create_next_playlist(get_pool(), theme=theme)

        # Log auto-closed playlists
        for closed in result.get("auto_closed", []):
            await log_action(
                get_pool(), "auto_close_playlist",
                turdom_number=closed["number"],
                playlist_id=closed["id"],
                triggered_by=message.from_user.id,
                result={"name": closed["name"], "reason": "create_next_theme"},
            )

        text = (
            f"✅ <b>Создан: {escape_html(result['name'])}</b>\n\n{escape_html(result['url'] or '')}\n\n"
            f"📎 Совместный доступ включён автоматически."
        )
        await reply(message, text)
        await offer_notification(result)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def setup_bot(pool):
    """Initialize bot: set pool, load tokens, recover session, start polling."""
    set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS spotify_tokens (
                id SERIAL PRIMARY KEY,
                refresh_token TEXT NOT NULL,
                access_token TEXT NOT NULL,
                expires_at TIMESTAMPTZ
            )
        """)

    await load_token_from_db(pool)

    # Set bot commands menu
    user_commands = [
        BotCommand(command="start", description="Справка по командам"),
        BotCommand(command="next", description="Следующий плейлист"),
        BotCommand(command="get", description="Ссылка на плейлист по номеру"),
        BotCommand(command="join", description="Присоединиться к сессии"),
        BotCommand(command="leave", description="Выйти из сессии"),
        BotCommand(command="secret", description="Оставить пасхалку"),
        BotCommand(command="check", description="Проверить дубликат"),
        BotCommand(command="stats", description="Общая статистика"),
        BotCommand(command="mystats", description="Моя статистика"),
        BotCommand(command="history", description="История сессий"),
        BotCommand(command="genres", description="Жанровые плейлисты"),
    ]
    await bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand(command="session", description="start / end / kick @user"),
        BotCommand(command="playlist", description="create / close / status / link / reschedule"),
        BotCommand(command="distribute", description="Раскидать треки по жанрам"),
        BotCommand(command="recap", description="Рекап сессии"),
        BotCommand(command="health", description="Здоровье бота"),
        BotCommand(command="preview", description="Превью карточки трека"),
        BotCommand(command="dbinfo", description="Инфо о базе"),
    ]
    await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=settings.telegram_admin_id))

    # Recover active session
    await session.recover()

    # Start duplicate watcher
    async def on_duplicate_found(telegram_id, track_title, artist, duplicates, playlist_name, track_id=None, added_by_name=None):
        async with pool.acquire() as conn:
            pl_row = await conn.fetchrow("SELECT url FROM playlists WHERE name = $1", playlist_name)
        msg = auto_removed_text(
            track_title, artist, track_id, duplicates,
            playlist_name=playlist_name, playlist_url=pl_row["url"] if pl_row else None, added_by_name=added_by_name,
        )

        if telegram_id:
            try:
                await send(telegram_id, msg)
            except Exception as e:
                log.debug(f"Failed to notify {telegram_id}: {e}")
        if telegram_id != settings.telegram_admin_id:
            try:
                await send(settings.telegram_admin_id, msg)
            except Exception as e:
                log.debug(f"Failed to notify {settings.telegram_admin_id}: {e}")

    async def on_fuzzy_duplicate_confirm(telegram_id, track_title, artist, duplicates, playlist_name, track_id, playlist_spotify_id, added_by_name=None):
        """Ask user to confirm fuzzy duplicate — show buttons."""
        from aiogram.types import InlineKeyboardMarkup

        msg = possible_duplicate_text(track_title, artist, track_id, duplicates, playlist_name, added_by_name)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            remove_duplicate_button(playlist_spotify_id, track_id, "🗑 Удалить"),
            keep_duplicate_button(playlist_spotify_id, track_id),
        ]])

        target = telegram_id or settings.telegram_admin_id
        try:
            await send(target, msg, reply_markup=kb)
        except Exception as e:
            log.debug(f"Failed to send fuzzy confirm to {target}: {e}")

    async def on_drop_warn(telegram_id, track_title, artist, drops, playlist_name, track_id, playlist_spotify_id):
        """Warn that a track was previously dropped."""
        from aiogram.types import InlineKeyboardMarkup

        msg = dropped_before_text(track_title, artist, track_id, drops, playlist_name)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            keep_duplicate_button(playlist_spotify_id, track_id),
            remove_duplicate_button(playlist_spotify_id, track_id, "🗑 Убрать"),
        ]])

        target = telegram_id or settings.telegram_admin_id
        try:
            await send(target, msg, reply_markup=kb)
        except Exception as e:
            log.debug(f"Failed to send drop warning to {target}: {e}")

    async def on_sibling_warn(telegram_id, track_title, artist, siblings, playlist_name, track_id, playlist_spotify_id):
        """Inform that a modified version (remix/sped up/etc) of an existing track was added — informational only."""
        msg = sibling_text(track_title, artist, track_id, siblings, playlist_name)
        target = telegram_id or settings.telegram_admin_id
        try:
            await send(target, msg)
        except Exception as e:
            log.debug(f"Failed to send sibling warning to {target}: {e}")

    set_duplicate_notify(on_duplicate_found)
    set_fuzzy_confirm(on_fuzzy_duplicate_confirm)
    watcher = DuplicateWatcher(
        pool, on_duplicate_found,
        confirm_callback=on_fuzzy_duplicate_confirm,
        drop_warn_callback=on_drop_warn,
        sibling_warn_callback=on_sibling_warn,
    )
    asyncio.create_task(watcher.start())

    init_health()
    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)
