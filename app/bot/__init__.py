"""Bot package — wires routers and provides setup_bot entry point."""

import asyncio
import html
import logging

from aiogram.types import Message, BotCommand, BotCommandScopeChat

from app.config import settings
from app.spotify.auth import load_token_from_db
from app.services.duplicate_watcher import DuplicateWatcher
from app.services.track_formatter import format_track

from app.bot.core import bot, dp, set_pool, is_admin, send, reply
from app.bot.session_manager import session
from app.bot.commands.user import router as user_router
from app.bot.commands.admin import router as admin_router, set_duplicate_notify, set_fuzzy_confirm, init_health
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
            f"✅ <b>Создан: {result['name']}</b>\n\n{result['url']}\n\n"
            f"📎 Совместный доступ включён автоматически."
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
        dup_links = []
        for d in duplicates:
            match_type = "🎯 точное" if d["match"] == "exact" else "🔗 ISRC"
            dup_links.append(f"  {match_type} — <a href=\"{d['url']}\">{d['playlist']}</a>")
        dup_text = "\n".join(dup_links)

        track_fmt = format_track(track_title, artist, track_id)

        async with pool.acquire() as conn:
            pl_row = await conn.fetchrow("SELECT url FROM playlists WHERE name = $1", playlist_name)
        removed_from = f"<a href=\"{pl_row['url']}\">{playlist_name}</a>" if pl_row and pl_row["url"] else playlist_name

        added_line = f"\n👤 Добавил: {html.escape(added_by_name)}" if added_by_name else ""
        msg = f"🗑 <b>Дубликат удалён!</b>\n\n🎵 {track_fmt}{added_line}\nУдалён из: {removed_from}\n\nУже был:\n{dup_text}"

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
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        match_labels = {
            "fuzzy_exact": "🔍 нормализованное совпадение",
            "fuzzy_contains": "🔍 слова совпадают",
            "fuzzy_levenshtein": "🔍 похожее название",
        }
        dup_links = []
        for d in duplicates:
            label = match_labels.get(d["match"], "🔍 похож")
            dup_links.append(f"  {label} — <a href=\"{d['url']}\">{d['playlist']}</a>\n  {d['title']} — {d['artist']}")
        dup_text = "\n".join(dup_links)

        track_fmt = format_track(track_title, artist, track_id)
        track_url = f"https://open.spotify.com/track/{track_id}" if track_id else ""

        added_line = f"\n👤 Добавил: {html.escape(added_by_name)}" if added_by_name else ""
        msg = (
            f"🔍 <b>Возможный дубликат</b>\n\n"
            f"🎵 {track_fmt}{added_line}\nв <b>{playlist_name}</b>\n\n"
            f"Похож на:\n{dup_text}\n\n"
            f"Удалить трек из плейлиста?"
        )

        # Encode callback data: confirm_dup:<playlist_spotify_id>:<track_id>
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_dup:{playlist_spotify_id}:{track_id}"),
            InlineKeyboardButton(text="✅ Оставить", callback_data=f"keep_dup:{track_id}"),
        ]])

        target = telegram_id or settings.telegram_admin_id
        try:
            await send(target, msg, reply_markup=kb)
        except Exception as e:
            log.debug(f"Failed to send fuzzy confirm to {target}: {e}")

    async def on_drop_warn(telegram_id, track_title, artist, drops, playlist_name, track_id, playlist_spotify_id):
        """Warn that a track was previously dropped."""
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        track_fmt = format_track(track_title, artist, track_id)
        drop_lines = [f"  ❌ {d['playlist']} ({d['date']}) — добавил {d['added_by']}" for d in drops]
        drop_text = "\n".join(drop_lines)

        msg = (
            f"⚠️ <b>Ранее дропнутый трек!</b>\n\n"
            f"🎵 {track_fmt}\nДобавлен в <b>{playlist_name}</b>\n\n"
            f"Был дропнут:\n{drop_text}\n\n"
            f"Оставить или убрать?"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Оставить", callback_data=f"keep_dup:{track_id}"),
            InlineKeyboardButton(text="🗑 Убрать", callback_data=f"confirm_dup:{playlist_spotify_id}:{track_id}"),
        ]])

        target = telegram_id or settings.telegram_admin_id
        try:
            await send(target, msg, reply_markup=kb)
        except Exception as e:
            log.debug(f"Failed to send drop warning to {target}: {e}")

    async def on_sibling_warn(telegram_id, track_title, artist, siblings, playlist_name, track_id, playlist_spotify_id):
        """Inform that a modified version (remix/sped up/etc) of an existing track was added — informational only."""
        track_fmt = format_track(track_title, artist, track_id)
        sib_lines = []
        kind_label = {"self": "это версия", "other": "уже есть версия", "both": "обе — версии"}
        for s in siblings[:5]:
            arrow = kind_label.get(s["kind"], "версия")
            sib_lines.append(
                f"  🔁 {arrow}: {html.escape(s['title'])} — {html.escape(s['artist'])} "
                f"<i>в {html.escape(s['playlist'])}</i>"
            )
        if len(siblings) > 5:
            sib_lines.append(f"  <i>…и ещё {len(siblings) - 5}</i>")
        sib_text = "\n".join(sib_lines)

        msg = (
            f"ℹ️ <b>Изменённая версия — не дубль</b>\n\n"
            f"🎵 {track_fmt}\nДобавлен в <b>{html.escape(playlist_name)}</b>\n\n"
            f"{sib_text}"
        )
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
