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
from app.services.playlists import import_playlist, import_all_turdom, check_duplicate, get_track_isrc, get_next_playlist, create_next_playlist, reschedule_playlist
from app.services.duplicate_watcher import DuplicateWatcher
from app.services.ai import generate_track_facts, generate_session_recap, generate_pre_recap_teaser
from app.services.genre_distributor import distribute_session_tracks

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
_cached_pre_recap: str | None = None


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


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>MaSpotifyBot</b> — бот для сессий TURDOM!\n\n"
        "Команды:\n"
        "/reg — привязать Spotify аккаунт\n"
        "/next — ссылка на следующий плейлист\n"
        "/check — проверить трек на дубликат\n"
        "/auth — подключить Spotify (админ)\n"
        "/session — начать сессию\n"
        "/end — завершить сессию\n"
        "/join — присоединиться к голосованию\n"
        "/setnextlink — установить invite-ссылку для плейлиста (админ)\n"
        "/import_all — импорт всех TURDOM плейлистов (админ)",
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


@dp.message(Command("reg"))
async def cmd_reg(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Регистрация доступна только через админа.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Скинь ссылку на свой Spotify профиль:\n\n"
            "<code>/reg https://open.spotify.com/user/YOUR_ID</code>\n\n"
            "Найти можно: Spotify → твой профиль → Share → Copy link",
            parse_mode="HTML",
        )
        return

    spotify_input = args[1].strip()
    spotify_id = _extract_spotify_user_id(spotify_input)
    if not spotify_id:
        await message.answer("Не могу распарсить Spotify ID. Скинь ссылку формата open.spotify.com/user/...")
        return

    tid = message.from_user.id
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, telegram_name, spotify_id, is_admin)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE SET spotify_id = $3, telegram_name = $2
            """,
            tid, message.from_user.full_name, spotify_id, is_admin(tid),
        )

    await message.answer(f"✅ Spotify привязан: <code>{spotify_id}</code>", parse_mode="HTML")


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return
    result = await get_next_playlist(_pool)
    if result:
        link = result.get("invite_url") or result["url"]
        await message.answer(
            f"🎧 <b>{result['name']}</b> ({result['status']})\n\n{link}",
            parse_mode="HTML",
        )
    else:
        await message.answer("Нет предстоящих плейлистов в базе.")


@dp.message(Command("setnextlink"))
async def cmd_setnextlink(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or "spotify.com/playlist" not in args[1]:
        await message.answer(
            "Скинь invite-ссылку на плейлист:\n<code>/setnextlink https://open.spotify.com/playlist/...?pt=...</code>",
            parse_mode="HTML",
        )
        return

    invite_url = args[1].strip()
    async with _pool.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE playlists SET invite_url = $1 WHERE status = 'upcoming' RETURNING name",
            invite_url,
        )

    if updated:
        await message.answer(f"✅ Invite-ссылка сохранена для <b>{updated}</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Нет upcoming плейлиста в базе.")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Скинь ссылку на трек:\n<code>/check https://open.spotify.com/track/...</code>",
            parse_mode="HTML",
        )
        return

    track_input = args[1].strip()
    match = re.search(r"track[/:]([a-zA-Z0-9]+)", track_input)
    track_id = match.group(1) if match else track_input

    await message.answer("🔍 Проверяю...")

    isrc = await get_track_isrc(track_id)
    duplicates = await check_duplicate(_pool, track_id, isrc)

    if duplicates:
        lines = []
        for d in duplicates:
            match_type = "🎯 точное совпадение" if d["match"] == "exact" else "🔗 тот же трек (другой альбом)"
            lines.append(f"• <b>{d['title']}</b> — {d['artist']}\n  {match_type} в {d['playlist']}\n  {d['url']}")
        await message.answer(
            f"⚠️ <b>Дубликат найден!</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML",
        )
    else:
        await message.answer("✅ Трек не найден в базе — можно добавлять!")


@dp.message(Command("scan"))
async def cmd_scan(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    await message.answer("🔍 Сканирую плейлисты на дубликаты...")
    try:
        watcher = DuplicateWatcher(_pool, _on_duplicate_notify)
        await watcher._check_playlists()
        await message.answer("✅ Сканирование завершено!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("import_all"))
async def cmd_import_all(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может импортировать.")
        return

    await message.answer("⏳ Сканирую Spotify и импортирую все TURDOM плейлисты... Это займёт пару минут.")

    try:
        results = await import_all_turdom(_pool)
        total_tracks = sum(r["tracks"] for r in results)
        text = f"✅ <b>Импорт завершён!</b>\n\nПлейлистов: {len(results)}\nТреков: {total_tracks}\n\n"
        for r in results[:20]:  # first 20
            text += f"• {r['name']} — {r['tracks']} треков\n"
        if len(results) > 20:
            text += f"\n...и ещё {len(results) - 20}"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка импорта: {e}")


@dp.message(Command("import"))
async def cmd_import(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может импортировать.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи ссылку: /import &lt;playlist_url&gt;")
        return

    playlist_id = _extract_playlist_id(args[1].strip())
    if not playlist_id:
        await message.answer("Не могу распарсить ID плейлиста.")
        return

    await message.answer("⏳ Импортирую...")
    try:
        result = await import_playlist(_pool, playlist_id)
        await message.answer(
            f"✅ <b>{result['name']}</b> — {result['tracks']} треков импортировано!",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("join"))
async def cmd_join(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return
    tid = message.from_user.id

    if is_admin(tid):
        if tid not in _participants:
            _participants.append(tid)
        if _active_session_id:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    _active_session_id, tid,
                )
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

        # Register admin as session participant
        await conn.execute(
            "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            _active_session_id, tid,
        )
        _played_track_ids = set()

    # Get playlist name + clear queue by starting playlist and pausing
    try:
        sp = await get_spotify()
        pl = await sp.playlist(playlist_id)
        playlist_name = pl.name
        # Start playlist context to reset queue, then pause immediately
        await sp.playback_start_context(f"spotify:playlist:{playlist_id}")
        await asyncio.sleep(0.5)
        await sp.playback_pause()
        log.info(f"Queue cleared for playlist {playlist_name}")
    except Exception as e:
        log.warning(f"Failed to clear queue: {e}")
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

    # Enable shuffle and resume playback (playlist already loaded at /session)
    try:
        sp = await get_spotify()
        await sp.playback_shuffle(True)
        await sp.playback_resume()
    except Exception as e:
        log.error(f"Failed to start playback: {e}")
        await callback.answer(f"Ошибка Spotify: {e}")
        return

    monitor.on_track_change(lambda info: _on_track_change(info))
    monitor.on_end(lambda: _on_session_end())

    # Pre-generate recap teaser in background
    async def _cache_teaser():
        global _cached_pre_recap
        try:
            sp_inner = await get_spotify()
            pl_items = await sp_inner.playlist_items(_active_playlist_id, limit=100)
            total = pl_items.total

            # Count tracks per contributor
            contributors = {}
            for item in pl_items.items:
                if item.added_by:
                    uid = item.added_by.id
                    contributors[uid] = contributors.get(uid, 0) + 1

            top_spotify_id = max(contributors, key=contributors.get) if contributors else None
            top_name = None
            if top_spotify_id:
                async with _pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT telegram_name FROM users WHERE spotify_id = $1", top_spotify_id)
                    if row:
                        top_name = row["telegram_name"]

            participant_names = []
            async with _pool.acquire() as conn:
                for tid in _participants:
                    row = await conn.fetchrow("SELECT telegram_name FROM users WHERE telegram_id = $1", tid)
                    if row:
                        participant_names.append(row["telegram_name"])

            _cached_pre_recap = await generate_pre_recap_teaser(total, participant_names, top_name)
            log.info(f"Pre-recap teaser cached: {_cached_pre_recap[:50]}...")
        except Exception as e:
            log.warning(f"Failed to generate pre-recap teaser: {e}")
            _cached_pre_recap = "🎧 Ну что, чем всё закончилось? Сейчас узнаем! 🥁"

    asyncio.create_task(_cache_teaser())

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

    session_id_to_end = _active_session_id
    await monitor.stop()

    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE session_tracks SET vote_result = 'keep' WHERE session_id = $1 AND vote_result = 'pending'",
            session_id_to_end,
        )
        await conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = NOW() WHERE id = $1",
            session_id_to_end,
        )

        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE vote_result = 'keep') as kept,
                COUNT(*) FILTER (WHERE vote_result = 'drop') as dropped
            FROM session_tracks WHERE session_id = $1
            """,
            session_id_to_end,
        )

    # Pre-recap teaser (cached from session start)
    teaser = _cached_pre_recap or "🎧 Ну что, чем всё закончилось? Сейчас узнаем! 🥁"
    for tid in _participants:
        try:
            await bot.send_message(tid, teaser, parse_mode="HTML")
            await bot.send_chat_action(tid, "typing")
        except Exception:
            pass

    recap = (
        f"📊 <b>Сессия завершена!</b>\n\n"
        f"Всего треков: {stats['total']}\n"
        f"✅ Оставлено: {stats['kept']}\n"
        f"❌ Удалено: {stats['dropped']}"
    )

    # AI recap
    async with _pool.acquire() as conn:
        tracks_data = [dict(r) for r in await conn.fetch(
            """
            SELECT st.title, st.artist, st.vote_result, st.added_by_spotify_id,
                   COALESCE(u.telegram_name, st.added_by_spotify_id, '?') as added_by
            FROM session_tracks st
            LEFT JOIN users u ON st.added_by_spotify_id = u.spotify_id
            WHERE st.session_id = $1
            """,
            session_id_to_end,
        )]
        participant_names = [r["telegram_name"] for r in await conn.fetch(
            "SELECT telegram_name FROM users WHERE telegram_id = ANY($1::bigint[])",
            _participants,
        )]

    ai_recap = await generate_session_recap(
        stats['total'], stats['kept'], stats['dropped'],
        tracks_data, participant_names,
    )
    if ai_recap:
        recap += f"\n\n🤖 <b>AI Recap:</b>\n{ai_recap}"

    for tid in _participants:
        try:
            await bot.send_message(tid, recap, parse_mode="HTML")
        except Exception:
            pass

    # Distribute kept tracks to genre playlists
    try:
        dist_result = await distribute_session_tracks(_pool, session_id_to_end)
        if dist_result["distributed"] > 0:
            dist_msg = f"🎶 Раскидал {dist_result['distributed']} треков по жанровым плейлистам!"
            for tid in _participants:
                try:
                    await bot.send_message(tid, dist_msg, parse_mode="HTML")
                except Exception:
                    pass
    except Exception as e:
        log.error(f"Genre distribution failed: {e}")

    # Mark current playlist as listened
    if _active_playlist_id:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE playlists SET status = 'listened' WHERE spotify_id = $1",
                _active_playlist_id,
            )

    # Offer to create next playlist (admin only)
    create_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Обычный", callback_data="create_playlist:normal"),
            InlineKeyboardButton(text="🎭 Тематический", callback_data="create_playlist:thematic"),
        ],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="create_playlist:skip")],
    ])
    await bot.send_message(
        settings.telegram_admin_id,
        "🆕 Создать следующий плейлист?",
        reply_markup=create_kb,
        parse_mode="HTML",
    )

    _active_session_id = None
    _active_playlist_id = None
    _current_session_track_id = None
    _played_track_ids = set()
    _track_messages.clear()
    _cached_pre_recap = None


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

    # Resolve added_by Spotify ID to Telegram name
    added_by_name = None
    if info.added_by:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_name FROM users WHERE spotify_id = $1", info.added_by
            )
            if row:
                added_by_name = row["telegram_name"]

    # Build links
    track_url = f"https://open.spotify.com/track/{info.track_id}"
    artist_name = info.artist.split(",")[0].strip()  # first artist for search link
    artist_search_url = f"https://open.spotify.com/search/{artist_name.replace(' ', '%20')}"

    if added_by_name:
        added_by_text = f"\n👤 {added_by_name}"
    elif info.added_by:
        added_by_text = f"\n👤 <code>{info.added_by}</code>"
    else:
        added_by_text = ""

    # Check cached AI facts first, then generate
    cached_facts = None
    async with _pool.acquire() as conn:
        cached_facts = await conn.fetchval(
            "SELECT ai_facts FROM playlist_tracks WHERE spotify_track_id = $1 AND ai_facts IS NOT NULL LIMIT 1",
            info.track_id,
        )

    if cached_facts:
        facts = cached_facts
    else:
        facts = await generate_track_facts(info.title, info.artist, info.album)
        # Cache for future
        if facts:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "UPDATE playlist_tracks SET ai_facts = $1 WHERE spotify_track_id = $2",
                    facts, info.track_id,
                )

    facts_text = f"\n\n💡 {facts}" if facts else ""

    text = (
        f"🎵 <a href=\"{track_url}\"><b>{info.title}</b></a>\n"
        f"🎤 <a href=\"{artist_search_url}\">{info.artist}</a>\n"
        f"💿 {info.album}"
        f"{added_by_text}{facts_text}"
    )

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
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    vote_type = parts[1]
    session_track_id = int(parts[2])

    result = await record_vote(_pool, session_track_id, callback.from_user.id, vote_type, session_id=_active_session_id)

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
                await bot.send_message(tid, "✅ Все проголосовали!", parse_mode="HTML")
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
            if _active_session_id:
                await conn.execute(
                    "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    _active_session_id, tid,
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


@dp.callback_query(F.data.startswith("create_playlist:"))
async def on_create_playlist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return

    action = callback.data.split(":")[1]

    if action == "skip":
        await callback.answer("Ок")
        await callback.message.edit_text("⏭ Создание плейлиста пропущено.", parse_mode="HTML")
        return

    if action == "thematic":
        await callback.answer("Напиши тему")
        await callback.message.edit_text(
            "🎭 Напиши тему для плейлиста (одним сообщением):",
            parse_mode="HTML",
        )
        # Set flag to catch next message as theme
        global _waiting_theme
        _waiting_theme = True
        return

    # Normal playlist
    await callback.answer("Создаю...")
    try:
        result = await create_next_playlist(_pool)
        text = (
            f"✅ <b>Создан: {result['name']}</b>\n\n{result['url']}\n\n"
            f"📎 Открой плейлист в Spotify → Invite Collaborators → скинь ссылку сюда:\n"
            f"<code>/setnextlink ссылка</code>"
        )
        await callback.message.edit_text(text, parse_mode="HTML")

        # Notify all participants
        for tid in _participants:
            if tid != callback.from_user.id:
                try:
                    await bot.send_message(
                        tid,
                        f"🆕 <b>Новый плейлист:</b> {result['name']}\n\nДобавляйте треки!\n{result['url']}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")


_waiting_theme = False


@dp.message(lambda m: _waiting_theme and is_admin(m.from_user.id))
async def on_theme_input(message: Message):
    global _waiting_theme
    if not _waiting_theme:
        return
    _waiting_theme = False

    theme = message.text.strip()
    try:
        result = await create_next_playlist(_pool, theme=theme)
        text = (
            f"✅ <b>Создан: {result['name']}</b>\n\n{result['url']}\n\n"
            f"📎 Открой плейлист в Spotify → Invite Collaborators → скинь ссылку сюда:\n"
            f"<code>/setnextlink ссылка</code>"
        )
        await message.answer(text, parse_mode="HTML")

        for tid in _participants:
            if tid != message.from_user.id:
                try:
                    await bot.send_message(
                        tid,
                        f"🆕 <b>Новый плейлист:</b> {result['name']}\n\nДобавляйте треки!\n{result['url']}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("reschedule"))
async def cmd_reschedule(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи новую дату: /reschedule 09/04/2026")
        return

    new_date = args[1].strip()
    if not re.match(r"\d{2}/\d{2}/\d{4}", new_date):
        await message.answer("Формат даты: ДД/ММ/ГГГГ")
        return

    result = await reschedule_playlist(_pool, new_date)
    if result:
        await message.answer(
            f"📅 Перенесено:\n<s>{result['old_name']}</s>\n→ <b>{result['new_name']}</b>",
            parse_mode="HTML",
        )
    else:
        await message.answer("Нет предстоящих плейлистов для переноса.")


def _extract_spotify_user_id(url_or_id: str) -> str | None:
    """Extract Spotify user ID from URL or raw ID."""
    # https://open.spotify.com/user/31xjkjxx...?si=...
    match = re.search(r"user[/:]([a-zA-Z0-9._-]+)", url_or_id)
    if match:
        return match.group(1)
    # Raw ID (no slashes, no spaces)
    if re.match(r"^[a-zA-Z0-9._-]+$", url_or_id) and "/" not in url_or_id:
        return url_or_id
    return None


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

    # Recover active session if bot restarted
    global _active_session_id, _active_playlist_id, _participants
    async with pool.acquire() as conn:
        active = await conn.fetchrow(
            "SELECT id, playlist_spotify_id FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        )
        if active:
            _active_session_id = active["id"]
            _active_playlist_id = active["playlist_spotify_id"]
            rows = await conn.fetch(
                "SELECT telegram_id FROM session_participants WHERE session_id = $1",
                _active_session_id,
            )
            _participants = [r["telegram_id"] for r in rows]
            log.info(f"Recovered active session {_active_session_id} with {len(_participants)} participants")

    # Start duplicate watcher in background
    global _on_duplicate_notify
    async def on_duplicate_found(telegram_id, track_title, artist, duplicates, playlist_name, track_id=None):
        dup_links = []
        for d in duplicates:
            match_type = "🎯 точное" if d["match"] == "exact" else "🔗 ISRC"
            dup_links.append(f"  {match_type} — <a href=\"{d['url']}\">{d['playlist']}</a>")
        dup_text = "\n".join(dup_links)

        track_link = f"https://open.spotify.com/track/{track_id}" if track_id else ""
        track_display = f"<a href=\"{track_link}\">{track_title}</a>" if track_id else track_title
        artist_search = f"https://open.spotify.com/search/{artist.replace(' ', '%20')}"
        artist_display = f"<a href=\"{artist_search}\">{artist}</a>"

        # Get playlist URL for where it was removed from
        async with pool.acquire() as conn:
            pl_row = await conn.fetchrow("SELECT url FROM playlists WHERE name = $1", playlist_name)
        removed_from = f"<a href=\"{pl_row['url']}\">{playlist_name}</a>" if pl_row and pl_row["url"] else playlist_name

        msg = f"🗑 <b>Дубликат удалён!</b>\n\n🎵 {track_display} — {artist_display}\nУдалён из: {removed_from}\n\nУже был:\n{dup_text}"

        if telegram_id:
            try:
                await bot.send_message(telegram_id, msg, parse_mode="HTML")
            except Exception:
                pass
        # Always notify admin too
        if telegram_id != settings.telegram_admin_id:
            try:
                await bot.send_message(settings.telegram_admin_id, msg, parse_mode="HTML")
            except Exception:
                pass

    _on_duplicate_notify = on_duplicate_found
    watcher = DuplicateWatcher(pool, on_duplicate_found)
    asyncio.create_task(watcher.start())

    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)
