"""Bot package — wires routers and provides setup_bot entry point."""

import asyncio
import logging

from aiogram.types import Message, BotCommand, BotCommandScopeChat

from app.config import settings
from app.spotify.auth import load_token_from_db
from app.services.duplicate_watcher import DuplicateWatcher
from app.services.track_formatter import format_track

from app.bot.core import bot, dp, set_pool, is_admin, send, reply
from app.bot.session_manager import session
from app.bot.commands.user import router as user_router
from app.bot.commands.admin import router as admin_router, set_duplicate_notify
from app.bot.callbacks import router as callbacks_router

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
    from app.bot.core import pool

    theme = message.text.strip()
    try:
        result = await create_next_playlist(pool, theme=theme)
        text = (
            f"✅ <b>Создан: {result['name']}</b>\n\n{result['url']}\n\n"
            f"📎 Открой плейлист в Spotify → Invite Collaborators → скинь ссылку сюда:\n"
            f"<code>/setnextlink ссылка</code>"
        )
        await reply(message, text)

        for tid in session.participants:
            if tid != message.from_user.id:
                try:
                    await send(
                        tid,
                        f"🆕 <b>Новый плейлист:</b> {result['name']}\n\nДобавляйте треки!\n{result['url']}",
                    )
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")
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
        BotCommand(command="distribute", description="Раскидать треки по жанрам"),
        BotCommand(command="recap", description="Рекап сессии"),
        BotCommand(command="close_playlist", description="Закрыть плейлист"),
        BotCommand(command="create_next", description="Создать следующий плейлист"),
        BotCommand(command="health", description="Статус плейлиста"),
        BotCommand(command="preview", description="Превью карточки трека"),
        BotCommand(command="dbinfo", description="Инфо о базе"),
    ]
    await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=settings.telegram_admin_id))

    # Recover active session
    await session.recover()

    # Start duplicate watcher
    async def on_duplicate_found(telegram_id, track_title, artist, duplicates, playlist_name, track_id=None):
        dup_links = []
        for d in duplicates:
            match_type = "🎯 точное" if d["match"] == "exact" else "🔗 ISRC"
            dup_links.append(f"  {match_type} — <a href=\"{d['url']}\">{d['playlist']}</a>")
        dup_text = "\n".join(dup_links)

        track_fmt = format_track(track_title, artist, track_id)

        async with pool.acquire() as conn:
            pl_row = await conn.fetchrow("SELECT url FROM playlists WHERE name = $1", playlist_name)
        removed_from = f"<a href=\"{pl_row['url']}\">{playlist_name}</a>" if pl_row and pl_row["url"] else playlist_name

        msg = f"🗑 <b>Дубликат удалён!</b>\n\n🎵 {track_fmt}\nУдалён из: {removed_from}\n\nУже был:\n{dup_text}"

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

    set_duplicate_notify(on_duplicate_found)
    watcher = DuplicateWatcher(pool, on_duplicate_found)
    asyncio.create_task(watcher.start())

    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)
