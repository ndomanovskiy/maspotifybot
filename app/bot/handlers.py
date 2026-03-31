import asyncio
import logging
import re

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.spotify.auth import start_oauth, exchange_code, run_oauth_callback_server, load_token_from_db, save_token_to_db, get_spotify
from app.spotify.monitor import SpotifyMonitor, TrackInfo
from app.services.voting import record_vote, remove_track_from_playlist, skip_to_next, create_session_track

log = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()
monitor = SpotifyMonitor()

# State
_pool: asyncpg.Pool | None = None
_active_session_id: int | None = None
_active_playlist_id: str | None = None
_current_session_track_id: int | None = None
_participants: list[int] = []  # telegram_ids
_track_messages: dict[int, list[tuple[int, int]]] = {}  # session_track_id -> [(chat_id, message_id)]
_played_track_ids: set[str] = set()  # spotify track IDs already played this session


def is_admin(telegram_id: int) -> bool:
    return telegram_id == settings.telegram_admin_id


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>MaSpotifyBot</b> — бот для сессий TURDOM!\n\n"
        "Команды:\n"
        "/auth — подключить Spotify\n"
        "/session — начать сессию\n"
        "/end — завершить сессию\n"
        "/join — присоединиться к голосованию",
        parse_mode="HTML",
    )


@dp.message(Command("auth"))
async def cmd_auth(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может подключить Spotify.")
        return

    url = await start_oauth()
    await message.answer(f"Перейди по ссылке для авторизации:\n\n{url}")

    async def on_code(code: str):
        token = await exchange_code(code)
        await save_token_to_db(_pool, token)
        await message.answer("✅ Spotify подключен!")

    asyncio.create_task(run_oauth_callback_server(on_code))


@dp.message(Command("join"))
async def cmd_join(message: Message):
    tid = message.from_user.id

    if is_admin(tid):
        if tid not in _participants:
            _participants.append(tid)
        await message.answer("✅ Ты админ, ты всегда в деле!")
        return

    if _active_session_id is None:
        await message.answer("Сейчас нет активной сессии. Подожди пока ведущий запустит /session")
        return

    if tid in _participants:
        await message.answer("Ты уже в сессии!")
        return

    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"approve:{tid}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{tid}"),
    ]])
    await bot.send_message(
        settings.telegram_admin_id,
        f"🙋 <b>{message.from_user.full_name}</b> (@{message.from_user.username or '—'}) хочет присоединиться. Пустить?",
        reply_markup=approve_kb,
        parse_mode="HTML",
    )
    await message.answer("⏳ Запрос отправлен ведущему. Жди подтверждения!")


@dp.message(Command("session"))
async def cmd_session(message: Message):
    global _active_session_id, _active_playlist_id, _played_track_ids

    if not is_admin(message.from_user.id):
        await message.answer("Только админ может запускать сессию.")
        return

    if _active_session_id is not None:
        await message.answer("Сессия уже идёт! Сначала /end")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи ссылку на плейлист: /session &lt;url&gt;")
        return

    playlist_input = args[1].strip()
    playlist_id = _extract_playlist_id(playlist_input)
    if not playlist_id:
        await message.answer("Не могу распарсить ID плейлиста. Скинь ссылку формата spotify.com/playlist/...")
        return

    # Auto-join admin
    tid = message.from_user.id
    if tid not in _participants:
        _participants.append(tid)
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (telegram_id, telegram_name, is_admin)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE SET telegram_name = $2
                """,
                tid, message.from_user.full_name, True,
            )

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO sessions (playlist_spotify_id, playlist_name) VALUES ($1, $2) RETURNING id",
            playlist_id, playlist_input[:100],
        )
        _active_session_id = row["id"]
        _active_playlist_id = playlist_id
        _played_track_ids = set()

    # Get playlist name + clear queue
    try:
        sp = await get_spotify()
        pl = await sp.playlist(playlist_id)
        playlist_name = pl.name
        # Clear Spotify queue by starting playlist and immediately pausing
        # This resets the queue to the playlist contents
    except Exception:
        playlist_name = playlist_id

    # Notify all registered users
    join_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Присоединиться!", callback_data="join_session")
    ]])
    async with _pool.acquire() as conn:
        all_users = await conn.fetch("SELECT telegram_id FROM users WHERE telegram_id != $1", message.from_user.id)
    for row in all_users:
        try:
            await bot.send_message(
                row["telegram_id"],
                f"🎶 <b>Новая сессия!</b>\n\n🎧 Плейлист: <b>{playlist_name}</b>\n\nХочешь присоединиться?",
                reply_markup=join_kb,
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Wait for admin to press Start
    start_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
    ]])
    await message.answer(
        f"🎧 Сессия создана: <b>{playlist_name}</b>\n"
        f"👥 Участников: {len(_participants)}\n\n"
        f"Жди пока все присоединятся, потом жми кнопку.",
        reply_markup=start_kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "start_listening")
async def on_start_listening(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return

    if _active_playlist_id is None:
        await callback.answer("Нет активной сессии!")
        return

    # Start playlist in Spotify with shuffle
    try:
        sp = await get_spotify()
        await sp.playback_shuffle(True)
        await sp.playback_start_context(f"spotify:playlist:{_active_playlist_id}")
    except Exception as e:
        log.error(f"Failed to start playback: {e}")
        await callback.answer(f"Ошибка Spotify: {e}")
        return

    monitor.on_track_change(lambda info: _on_track_change(info))
    monitor.on_end(lambda: _on_session_end())

    await callback.answer("▶️ Поехали!")
    await callback.message.edit_text(
        f"▶️ <b>Прослушивание запущено!</b> Участников: {len(_participants)}",
        parse_mode="HTML",
    )

    for tid in _participants:
        if tid != callback.from_user.id:
            try:
                await bot.send_message(tid, "▶️ <b>Прослушивание началось!</b> Голосуй за треки!", parse_mode="HTML")
            except Exception:
                pass

    asyncio.create_task(monitor.start(_active_playlist_id))


@dp.message(Command("end"))
async def cmd_end(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может завершить сессию.")
        return

    if _active_session_id is None:
        await message.answer("Нет активной сессии.")
        return

    await _end_session()


async def _end_session():
    global _active_session_id, _active_playlist_id, _current_session_track_id, _played_track_ids

    if _active_session_id is None:
        return

    await monitor.stop()

    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE session_tracks SET vote_result = 'keep' WHERE session_id = $1 AND vote_result = 'pending'",
            _active_session_id,
        )
        await conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = NOW() WHERE id = $1",
            _active_session_id,
        )

        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE vote_result = 'keep') as kept,
                COUNT(*) FILTER (WHERE vote_result = 'drop') as dropped
            FROM session_tracks WHERE session_id = $1
            """,
            _active_session_id,
        )

    recap = (
        f"📊 <b>Сессия завершена!</b>\n\n"
        f"Всего треков: {stats['total']}\n"
        f"✅ Оставлено: {stats['kept']}\n"
        f"❌ Удалено: {stats['dropped']}"
    )

    for tid in _participants:
        try:
            await bot.send_message(tid, recap, parse_mode="HTML")
        except Exception:
            pass

    _active_session_id = None
    _active_playlist_id = None
    _current_session_track_id = None
    _played_track_ids = set()
    _track_messages.clear()


async def _on_session_end():
    log.info("Playlist ended — auto-ending session")
    await _end_session()


async def _on_track_change(info: TrackInfo):
    global _current_session_track_id

    if _active_session_id is None:
        return

    # Playlist looped — end session
    if info.track_id in _played_track_ids:
        log.info(f"Track {info.track_id} already played — ending session")
        await _end_session()
        return

    _played_track_ids.add(info.track_id)

    session_track_id = await create_session_track(_pool, _active_session_id, info)
    _current_session_track_id = session_track_id

    added_by_text = f"\n👤 Added by: <code>{info.added_by}</code>" if info.added_by else ""
    text = f"🎵 <b>{info.title}</b>\n🎤 {info.artist}\n💿 {info.album}{added_by_text}"

    vote_row = [
        InlineKeyboardButton(text="✅ Keep", callback_data=f"vote:keep:{session_track_id}"),
        InlineKeyboardButton(text="❌ Drop", callback_data=f"vote:drop:{session_track_id}"),
    ]

    sent_messages = []
    for tid in _participants:
        try:
            if is_admin(tid):
                rows = [vote_row, [InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{session_track_id}")]]
            else:
                rows = [vote_row]
            kb = InlineKeyboardMarkup(inline_keyboard=rows)

            if info.cover_url:
                msg = await bot.send_photo(tid, photo=info.cover_url, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                msg = await bot.send_message(tid, text, reply_markup=kb, parse_mode="HTML")
            sent_messages.append((tid, msg.message_id))
        except Exception as e:
            log.warning(f"Failed to send track to {tid}: {e}")

    _track_messages[session_track_id] = sent_messages


async def _update_vote_buttons(session_track_id: int):
    if session_track_id not in _track_messages:
        return

    async with _pool.acquire() as conn:
        keep_count = await conn.fetchval(
            "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'keep'", session_track_id
        )
        drop_count = await conn.fetchval(
            "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'drop'", session_track_id
        )

    keep_text = f"✅ Keep ({keep_count})" if keep_count > 0 else "✅ Keep"
    drop_text = f"❌ Drop ({drop_count})" if drop_count > 0 else "❌ Drop"

    for chat_id, message_id in _track_messages[session_track_id]:
        try:
            vote_row = [
                InlineKeyboardButton(text=keep_text, callback_data=f"vote:keep:{session_track_id}"),
                InlineKeyboardButton(text=drop_text, callback_data=f"vote:drop:{session_track_id}"),
            ]
            if is_admin(chat_id):
                rows = [vote_row, [InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{session_track_id}")]]
            else:
                rows = [vote_row]
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass


@dp.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    vote_type = parts[1]
    session_track_id = int(parts[2])

    result = await record_vote(_pool, session_track_id, callback.from_user.id, vote_type)

    if result["status"] == "already_voted":
        await callback.answer("Ты уже голосовал за этот трек!")
        return

    if result["status"] == "vote_changed":
        emoji = "✅" if vote_type == "keep" else "❌"
        await callback.answer(f"{emoji} Голос изменён!")
    else:
        emoji = "✅" if vote_type == "keep" else "❌"
        await callback.answer(f"{emoji} Голос засчитан!")

    # Update buttons with counts
    await _update_vote_buttons(session_track_id)

    # Drop — remove + skip immediately
    if result["status"] == "dropped":
        for tid in _participants:
            try:
                await bot.send_message(
                    tid,
                    f"🗑 <b>Трек удалён!</b> ({result['drop_count']} drop из {result['participants']} участников)",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        if _active_playlist_id:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT spotify_track_id FROM session_tracks WHERE id = $1", session_track_id
                )
            if row:
                await remove_track_from_playlist(_active_playlist_id, row["spotify_track_id"])
                await skip_to_next()
        return

    # All voted (any result) — auto-skip
    if result["total_votes"] >= len(_participants):
        for tid in _participants:
            try:
                await bot.send_message(tid, "✅ Все проголосовали — следующий трек!", parse_mode="HTML")
            except Exception:
                pass
        await skip_to_next()


@dp.callback_query(F.data.startswith("skip:"))
async def on_skip(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий может скипать!")
        return

    await callback.answer("⏭ Скипаю...")
    await skip_to_next()


@dp.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return

    tid = int(callback.data.split(":")[1])
    if tid not in _participants:
        _participants.append(tid)
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, telegram_name) VALUES ($1, $2) ON CONFLICT (telegram_id) DO NOTHING",
                tid, "",
            )

    await callback.answer("✅ Одобрено!")
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ Одобрено!", parse_mode="HTML")
    try:
        await bot.send_message(tid, f"✅ Ты в деле! Участников: {len(_participants)}")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("deny:"))
async def on_deny(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return

    tid = int(callback.data.split(":")[1])
    await callback.answer("❌ Отказано")
    await callback.message.edit_text(f"{callback.message.text}\n\n❌ Отказано", parse_mode="HTML")
    try:
        await bot.send_message(tid, "❌ Ведущий не одобрил присоединение.")
    except Exception:
        pass


@dp.callback_query(F.data == "join_session")
async def on_join_session(callback: CallbackQuery):
    tid = callback.from_user.id

    if tid in _participants:
        await callback.answer("Ты уже в сессии!")
        return

    if _active_session_id is None:
        await callback.answer("Сессия уже закончилась!")
        return

    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"approve:{tid}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{tid}"),
    ]])
    await bot.send_message(
        settings.telegram_admin_id,
        f"🙋 <b>{callback.from_user.full_name}</b> (@{callback.from_user.username or '—'}) хочет присоединиться. Пустить?",
        reply_markup=approve_kb,
        parse_mode="HTML",
    )
    await callback.answer("⏳ Запрос отправлен ведущему!")
    await callback.message.edit_text(
        f"{callback.message.text}\n\n⏳ Запрос отправлен, жди подтверждения...",
        parse_mode="HTML",
    )


def _extract_playlist_id(url_or_id: str) -> str | None:
    match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url_or_id)
    if match:
        return match.group(1)
    if re.match(r"^[a-zA-Z0-9]{22}$", url_or_id):
        return url_or_id
    return None


async def setup_bot(pool: asyncpg.Pool):
    global _pool
    _pool = pool

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

    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)
