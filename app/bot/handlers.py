import asyncio
import logging
import re

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from app.config import settings
from app.spotify.auth import start_oauth, exchange_code, run_oauth_callback_server, load_token_from_db, save_token_to_db, get_spotify
from app.spotify.monitor import SpotifyMonitor, TrackInfo
from app.services.voting import record_vote, remove_track_from_playlist, skip_to_next, create_session_track
from app.services.playlists import import_playlist, import_all_turdom, check_duplicate, get_track_isrc, get_next_playlist, create_next_playlist, reschedule_playlist
from app.services.duplicate_watcher import DuplicateWatcher
from app.services.ai import generate_track_facts, generate_pre_recap_teaser
from app.services.genre_distributor import distribute_session_tracks
from app.services.genre_resolver import backfill_genres
from app.services.track_formatter import format_track, format_track_plain, format_album
from app.services.admin_commands import (
    cmd_distribute, cmd_distribute_force, cmd_recap, cmd_recap_regenerate,
    cmd_close_playlist, cmd_create_next, cmd_dbinfo, log_action, check_duplicate_session,
)

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
_skip_in_progress: set[int] = set()  # session_track_ids currently being skipped (race condition guard)
_session_message: tuple[int, int] | None = None  # (chat_id, message_id) of session creation message


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
    # Handle deeplinks like /start history_7
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("history_"):
        session_id = args[1].replace("history_", "")
        if session_id.isdigit():
            await _show_session_details(message, int(session_id))
            return

    msg = (
        "🎵 <b>TURDOM Assistant</b>\n\n"
        "📋 <b>Команды:</b>\n"
        "/reg — привязать Spotify аккаунт\n"
        "/next — ссылка на следующий плейлист\n"
        "/join — присоединиться к сессии\n"
        "/leave — выйти из сессии\n"
        "/stats — общая статистика TURDOM\n"
        "/mystats — твоя персональная статистика\n"
        "/history — история сессий\n"
        "/check — проверить трек на дубликат"
    )

    if is_admin(message.from_user.id):
        msg += (
            "\n\n🔧 <b>Админ:</b>\n"
            "/session — начать сессию\n"
            "/end — завершить сессию\n"
            "/kick — кикнуть участника\n"
            "/distribute — раскидать треки по жанрам\n"
            "/recap — рекап сессии\n"
            "/close_playlist — закрыть плейлист\n"
            "/create_next — создать следующий плейлист\n"
            "/setnextlink — invite-ссылка для плейлиста\n"
            "/reschedule — перенести дату плейлиста\n"
            "/backfill_genres — заполнить жанры\n"
            "/dbinfo — инфо о базе\n"
            "/import_all — импорт всех TURDOM плейлистов\n"
            "/scan — принудительный скан дубликатов\n"
            "/auth — подключить Spotify"
        )

    await message.answer(msg, parse_mode="HTML")


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
            INSERT INTO users (telegram_id, telegram_name, telegram_username, spotify_id, is_admin)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (telegram_id) DO UPDATE SET spotify_id = $4, telegram_name = $2, telegram_username = $3
            """,
            tid, message.from_user.full_name, message.from_user.username or "", spotify_id, is_admin(tid),
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
            track_display = format_track(d["title"], d["artist"])
            lines.append(f"• {track_display}\n  {match_type} в {d['playlist']}\n  {d['url']}")
        await message.answer(
            f"⚠️ <b>Дубликат найден!</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML", link_preview_options=_NO_PREVIEW,
        )
    else:
        await message.answer("✅ Трек не найден в базе — можно добавлять!")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return

    async with _pool.acquire() as conn:
        # Total tracks and playlists
        total_tracks = await conn.fetchval("SELECT COUNT(*) FROM playlist_tracks")
        total_playlists = await conn.fetchval("SELECT COUNT(*) FROM playlists WHERE number IS NOT NULL")
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE spotify_id IS NOT NULL")

        # Per-user stats
        user_rows = await conn.fetch("""
            SELECT COALESCE(u.telegram_username, u.telegram_name) as name,
                   COUNT(pt.id) as tracks
            FROM users u
            LEFT JOIN playlist_tracks pt ON u.spotify_id = pt.added_by_spotify_id
            WHERE u.spotify_id IS NOT NULL
            GROUP BY name ORDER BY tracks DESC
        """)

        # Genre breakdown
        genre_rows = await conn.fetch("""
            SELECT genre, COUNT(*) as cnt
            FROM playlist_tracks
            WHERE genre IS NOT NULL AND genre <> ''
            GROUP BY genre ORDER BY cnt DESC
        """)

    # Classify genres into TURDOM playlists
    from app.services.genre_distributor import classify_track, GENRE_MAP
    genre_totals: dict[str, int] = {}
    for r in genre_rows:
        playlist = classify_track(r["genre"])
        if playlist:
            short = playlist.replace("TURDOM ", "")
            genre_totals[short] = genre_totals.get(short, 0) + r["cnt"]

    genre_emojis = {
        "Electronic": "⚡", "Pop": "🎹", "Metal": "🤘", "Rock": "🎸",
        "Hip-Hop": "🎤", "Indie": "🎶", "DnB": "🥁", "R&B": "💜",
        "Chill": "🌊", "Soundtrack": "🎬", "Phonk": "👻",
    }

    # Build messages
    msg1 = (
        f"🎵 <b>TURDOM STATS</b>\n"
        f"<i>{total_tracks} треков · {total_playlists} сессий · {total_users} участников</i>\n\n"
        f"<b>📊 Жанровые плейлисты:</b>\n\n"
    )
    for name in ["Electronic", "Pop", "Metal", "Rock", "Hip-Hop", "Indie", "DnB", "R&B", "Chill", "Soundtrack", "Phonk"]:
        count = genre_totals.get(name, 0)
        emoji = genre_emojis.get(name, "")
        msg1 += f"{emoji} {name} — {count}\n"

    # Per-user genre breakdown
    async with _pool.acquire() as conn:
        all_user_genres = await conn.fetch("""
            SELECT COALESCE(u.telegram_username, u.telegram_name) as name,
                   pt.genre, COUNT(*) as cnt
            FROM playlist_tracks pt
            JOIN users u ON u.spotify_id = pt.added_by_spotify_id
            WHERE pt.genre IS NOT NULL AND pt.genre <> ''
            GROUP BY name, pt.genre ORDER BY name, cnt DESC
        """)

    # Group by user
    user_genre_map: dict[str, dict[str, int]] = {}
    for r in all_user_genres:
        name = r["name"]
        if name not in user_genre_map:
            user_genre_map[name] = {}
        pl = classify_track(r["genre"])
        if pl:
            short = pl.replace("TURDOM ", "")
            user_genre_map[name][short] = user_genre_map[name].get(short, 0) + r["cnt"]

    msg2 = "👤 <b>Кто что слушает</b>\n\n"
    for r in user_rows:
        name = r["name"]
        tracks = r["tracks"]
        user_genres = user_genre_map.get(name, {})
        top3 = sorted(user_genres.items(), key=lambda x: -x[1])[:3]
        top3_str = " · ".join(f"{g} {c}" for g, c in top3)
        msg2 += f"@{name} — {tracks} треков\n<code>{top3_str}</code>\n\n"

    await message.answer(msg1, parse_mode="HTML")
    await message.answer(msg2, parse_mode="HTML")


@dp.message(Command("mystats"))
async def cmd_mystats(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return

    tid = message.from_user.id
    async with _pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT spotify_id, telegram_username, telegram_name FROM users WHERE telegram_id = $1", tid
        )
        if not user or not user["spotify_id"]:
            await message.answer("У тебя не привязан Spotify. Используй /reg")
            return

        spotify_id = user["spotify_id"]
        display = f"@{user['telegram_username']}" if user["telegram_username"] else user["telegram_name"]

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM playlist_tracks WHERE added_by_spotify_id = $1", spotify_id
        )

        # Sessions participated
        sessions_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_participants WHERE telegram_id = $1", tid
        )

        # Voting stats
        votes_kept = await conn.fetchval("""
            SELECT COUNT(*) FROM session_tracks st
            JOIN votes v ON v.session_track_id = st.id
            WHERE st.added_by_spotify_id = $1 AND st.vote_result = 'keep'
        """, spotify_id) or 0
        votes_dropped = await conn.fetchval("""
            SELECT COUNT(*) FROM session_tracks st
            JOIN votes v ON v.session_track_id = st.id
            WHERE st.added_by_spotify_id = $1 AND st.vote_result = 'drop'
        """, spotify_id) or 0

        # Genre breakdown
        genre_rows = await conn.fetch("""
            SELECT genre, COUNT(*) as cnt FROM playlist_tracks
            WHERE added_by_spotify_id = $1 AND genre IS NOT NULL AND genre <> ''
            GROUP BY genre ORDER BY cnt DESC
        """, spotify_id)

    from app.services.genre_distributor import classify_track
    genre_totals: dict[str, int] = {}
    for r in genre_rows:
        pl = classify_track(r["genre"])
        if pl:
            short = pl.replace("TURDOM ", "")
            genre_totals[short] = genre_totals.get(short, 0) + r["cnt"]

    top5 = sorted(genre_totals.items(), key=lambda x: -x[1])[:5]
    top_genre = top5[0][0] if top5 else "—"

    genre_emojis = {
        "Electronic": "⚡", "Pop": "🎹", "Metal": "🤘", "Rock": "🎸",
        "Hip-Hop": "🎤", "Indie": "🎶", "DnB": "🥁", "R&B": "💜",
        "Chill": "🌊", "Soundtrack": "🎬", "Phonk": "👻",
    }
    top_emoji = genre_emojis.get(top_genre, "🎵")

    msg = (
        f"📊 <b>Статистика {display}</b>\n\n"
        f"🎵 Треков добавлено: <b>{total}</b>\n"
        f"📅 Сессий: <b>{sessions_count}</b>\n"
        f"✅ Осталось: <b>{votes_kept}</b> · ❌ Удалено: <b>{votes_dropped}</b>\n\n"
        f"<b>Топ жанры:</b>\n"
    )
    for g_name, g_count in top5:
        emoji = genre_emojis.get(g_name, "")
        msg += f"{emoji} {g_name} — {g_count}\n"

    msg += f"\n{top_emoji} <b>Профиль: {top_genre} Lover</b>"

    await message.answer(msg, parse_mode="HTML")


HISTORY_PAGE_SIZE = 5


@dp.message(Command("history"))
async def cmd_history(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return

    args = message.text.split(maxsplit=1)

    # /history N — details for a specific session
    if len(args) > 1 and args[1].strip().isdigit():
        session_num = int(args[1].strip())
        await _show_session_details(message, session_num)
        return

    # /history — paginated list, page 1
    await _show_history_page(message, offset=0)


async def _show_history_page(message_or_callback, offset: int):
    async with _pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM sessions")
        sessions = await conn.fetch("""
            SELECT s.id, s.playlist_name, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM session_tracks WHERE session_id = s.id) as track_count,
                   (SELECT COUNT(*) FROM session_tracks WHERE session_id = s.id AND vote_result = 'keep') as kept,
                   (SELECT COUNT(*) FROM session_tracks WHERE session_id = s.id AND vote_result = 'drop') as dropped,
                   (SELECT COUNT(*) FROM session_participants WHERE session_id = s.id) as participants
            FROM sessions s
            ORDER BY s.started_at DESC
            LIMIT $1 OFFSET $2
        """, HISTORY_PAGE_SIZE, offset)

    if not sessions:
        text = "📅 Нет сессий в истории."
        if hasattr(message_or_callback, 'answer'):
            await message_or_callback.answer(text)
        else:
            await message_or_callback.message.edit_text(text, parse_mode="HTML")
        return

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    lines = [f"📅 <b>История сессий</b> ({offset + 1}–{min(offset + HISTORY_PAGE_SIZE, total)} из {total})\n"]
    for s in sessions:
        name = s["playlist_name"]
        if "TURDOM" not in name and "playlist" in name.lower():
            name = f"Сессия #{s['id']}"
        date = s["started_at"].strftime("%d/%m/%Y") if s["started_at"] else "?"
        tracks = s["track_count"]
        kept = s["kept"]
        dropped = s["dropped"]
        parts = s["participants"]
        deeplink = f"https://t.me/{bot_username}?start=history_{s['id']}"

        lines.append(
            f"<a href=\"{deeplink}\"><b>{name}</b></a>\n"
            f"📆 {date} · 🎵 {tracks} треков · 👥 {parts}\n"
            f"✅ {kept} осталось · ❌ {dropped} удалено\n"
        )

    text = "\n".join(lines)

    # Pagination buttons
    buttons = []
    if offset > 0:
        buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"history:{offset - HISTORY_PAGE_SIZE}"))
    if offset + HISTORY_PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="▶️ Далее", callback_data=f"history:{offset + HISTORY_PAGE_SIZE}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    if hasattr(message_or_callback, 'answer'):
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("history:"))
async def on_history_page(callback: CallbackQuery):
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return
    offset = int(callback.data.split(":")[1])
    await callback.answer()
    await _show_history_page(callback, offset=max(0, offset))


async def _show_session_details(message: Message, session_num: int):
    """Show detailed view of a specific session."""
    async with _pool.acquire() as conn:
        session = await conn.fetchrow("""
            SELECT s.id, s.playlist_name, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM session_participants WHERE session_id = s.id) as participants
            FROM sessions s WHERE s.id = $1
        """, session_num)

        if not session:
            await message.answer(f"Сессия #{session_num} не найдена.")
            return

        tracks = await conn.fetch("""
            SELECT st.spotify_track_id, st.title, st.artist, st.vote_result,
                   COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name, '?') as added_by
            FROM session_tracks st
            LEFT JOIN users u ON st.added_by_spotify_id = u.spotify_id
            WHERE st.session_id = $1
            ORDER BY st.position, st.id
        """, session_num)

    name = session["playlist_name"]
    date = session["started_at"].strftime("%d/%m/%Y %H:%M") if session["started_at"] else "?"
    parts = session["participants"]

    lines = [
        f"📅 <b>{name}</b>",
        f"📆 {date} · 👥 {parts} участников\n",
    ]

    for t in tracks:
        icon = "✅" if t["vote_result"] == "keep" else "❌" if t["vote_result"] == "drop" else "⏳"
        track_display = format_track(t["title"], t["artist"], t["spotify_track_id"])
        lines.append(f"{icon} {track_display} · 👤 {t['added_by']}")

    kept = sum(1 for t in tracks if t["vote_result"] == "keep")
    dropped = sum(1 for t in tracks if t["vote_result"] == "drop")
    lines.append(f"\n🎵 {len(tracks)} треков · ✅ {kept} осталось · ❌ {dropped} удалено")

    await message.answer("\n".join(lines), parse_mode="HTML", link_preview_options=_NO_PREVIEW)


@dp.message(Command("scan"))
async def cmd_scan(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    await message.answer("🔍 Сканирую upcoming плейлист на дубликаты...")
    try:
        from app.services.playlists import check_duplicate, get_track_isrc
        from app.spotify.auth import get_spotify

        async with _pool.acquire() as conn:
            playlists = await conn.fetch(
                "SELECT id, spotify_id, name FROM playlists WHERE status IN ('active', 'upcoming')"
            )

        if not playlists:
            await message.answer("Нет active/upcoming плейлистов.")
            return

        sp = await get_spotify()
        found_count = 0

        for pl in playlists:
            items = await sp.playlist_items(pl["spotify_id"], limit=100)
            for item in items.items:
                if not item.track:
                    continue
                track = item.track
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)
                if not isrc:
                    isrc = await get_track_isrc(track.id)

                duplicates = await check_duplicate(_pool, track.id, isrc)
                duplicates = [d for d in duplicates if d["playlist"] != pl["name"]]

                if duplicates:
                    found_count += 1
                    # Auto-remove
                    await sp.playlist_remove(pl["spotify_id"], [f"spotify:track:{track.id}"])
                    async with _pool.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM playlist_tracks WHERE playlist_id = $1 AND spotify_track_id = $2",
                            pl["id"], track.id,
                        )
                    await _on_duplicate_notify(
                        telegram_id=None,
                        track_title=track.name,
                        artist=", ".join(a.name for a in track.artists),
                        duplicates=duplicates,
                        playlist_name=pl["name"],
                        track_id=track.id,
                    )

        await message.answer(f"✅ Сканирование завершено! Дубликатов найдено и удалено: {found_count}")
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


@dp.message(Command("leave"))
async def cmd_leave(message: Message):
    if not await is_registered(message.from_user.id):
        await message.answer("⛔ Доступ только для участников TURDOM.")
        return
    tid = message.from_user.id

    if tid not in _participants:
        await message.answer("Ты не в сессии.")
        return

    _participants.remove(tid)
    if _active_session_id:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE session_participants SET active = FALSE, left_at = NOW() WHERE session_id = $1 AND telegram_id = $2",
                _active_session_id, tid,
            )
    await message.answer(f"👋 Ты вышел из сессии. Участников: {len(_participants)}")


@dp.message(Command("kick"))
async def cmd_kick(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи @username: /kick @username")
        return

    username = args[1].strip().lstrip("@")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM users WHERE telegram_username = $1", username
        )

    if not row:
        await message.answer(f"Юзер @{username} не найден в базе.")
        return

    tid = row["telegram_id"]
    if tid not in _participants:
        await message.answer(f"@{username} не в текущей сессии.")
        return

    _participants.remove(tid)
    if _active_session_id:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE session_participants SET active = FALSE, left_at = NOW() WHERE session_id = $1 AND telegram_id = $2",
                _active_session_id, tid,
            )
    await message.answer(f"👢 @{username} кикнут из сессии. Участников: {len(_participants)}")
    try:
        await bot.send_message(tid, "👢 Тебя убрали из текущей сессии.")
    except Exception:
        pass


async def _get_participant_names() -> str:
    """Get formatted list of participant names."""
    names = []
    async with _pool.acquire() as conn:
        for tid in _participants:
            row = await conn.fetchrow(
                "SELECT telegram_username, telegram_name FROM users WHERE telegram_id = $1", tid
            )
            if row:
                name = f"@{row['telegram_username']}" if row["telegram_username"] else row["telegram_name"]
                names.append(name)
            else:
                names.append(str(tid))
    return ", ".join(names) if names else "—"


async def _update_session_message():
    """Update the session creation message with current participant list."""
    if not _session_message or not _active_session_id:
        return
    try:
        chat_id, msg_id = _session_message
        start_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
        ]])
        async with _pool.acquire() as conn:
            playlist_name = await conn.fetchval(
                "SELECT playlist_name FROM sessions WHERE id = $1", _active_session_id
            )
        names = await _get_participant_names()
        await bot.edit_message_text(
            f"🎧 Сессия создана: <b>{playlist_name}</b>\n"
            f"👥 Участников: {len(_participants)} — {names}\n\n"
            f"Жди пока все присоединятся, потом жми кнопку.",
            chat_id=chat_id, message_id=msg_id,
            reply_markup=start_kb, parse_mode="HTML",
        )
    except Exception:
        pass


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
                INSERT INTO users (telegram_id, telegram_name, telegram_username, is_admin)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (telegram_id) DO UPDATE SET telegram_name = $2, telegram_username = $3
                """,
                tid, message.from_user.full_name, message.from_user.username or "", True,
            )

    # Get playlist name from Spotify
    try:
        sp = await get_spotify()
        pl_info = await sp.playlist(playlist_id)
        playlist_name = pl_info.name
    except Exception:
        playlist_name = playlist_input[:100]

    # Check if a session already exists for this playlist
    if await check_duplicate_session(_pool, playlist_id):
        await message.answer(
            f"🚫 Для этого плейлиста уже есть сессия! Нельзя создать вторую.",
            parse_mode="HTML",
        )
        return

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO sessions (playlist_spotify_id, playlist_name) VALUES ($1, $2) RETURNING id",
            playlist_id, playlist_name,
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
    global _session_message
    start_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
    ]])
    names = await _get_participant_names()
    session_msg = await message.answer(
        f"🎧 Сессия создана: <b>{playlist_name}</b>\n"
        f"👥 Участников: {len(_participants)} — {names}\n\n"
        f"Жди пока все присоединятся, потом жми кнопку.",
        reply_markup=start_kb,
        parse_mode="HTML",
    )
    _session_message = (session_msg.chat.id, session_msg.message_id)


@dp.callback_query(F.data == "start_listening")
async def on_start_listening(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return

    if _active_playlist_id is None:
        await callback.answer("Нет активной сессии!")
        return

    # Enable shuffle and start playback with explicit device
    try:
        sp = await get_spotify()
        devices = await sp.playback_devices()
        if not devices:
            await callback.answer("❌ Открой Spotify на устройстве и нажми ещё раз!", show_alert=True)
            return
        device_id = devices[0].id
        await sp.playback_shuffle(True, device_id=device_id)
        await sp.playback_start_context(f"spotify:playlist:{_active_playlist_id}", device_id=device_id)
    except Exception as e:
        log.error(f"Failed to start playback: {e}")
        await callback.answer(f"Ошибка Spotify: {e}", show_alert=True)
        return

    monitor.on_track_change(lambda info: _on_track_change(info))

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
    global _active_session_id, _active_playlist_id, _current_session_track_id, _played_track_ids, _cached_pre_recap, _session_message

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
                   COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name, st.added_by_spotify_id, '?') as added_by
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
        # Save recap to DB
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET recap_text = $1 WHERE id = $2",
                ai_recap, session_id_to_end,
            )

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
        # Mark as distributed
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET distributed_at = NOW() WHERE id = $1",
                session_id_to_end,
            )
    except Exception as e:
        log.error(f"Genre distribution failed: {e}")

    # Log end_session action
    try:
        await log_action(
            _pool, "end_session",
            session_id=session_id_to_end,
            result={"total": stats["total"], "kept": stats["kept"], "dropped": stats["dropped"]},
        )
    except Exception:
        pass

    # Note: playlist is NOT auto-closed anymore — use /close_playlist command
    # Offer admin to run post-session commands
    post_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Обычный", callback_data="create_playlist:normal"),
         InlineKeyboardButton(text="🎭 Тематический", callback_data="create_playlist:thematic")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="create_playlist:skip")],
    ])
    await bot.send_message(
        settings.telegram_admin_id,
        "🆕 Создать следующий плейлист?",
        reply_markup=post_kb,
        parse_mode="HTML",
    )

    _active_session_id = None
    _active_playlist_id = None
    _current_session_track_id = None
    _played_track_ids = set()
    _track_messages.clear()
    _cached_pre_recap = None
    _skip_in_progress.clear()
    _session_message = None


async def _on_track_change(info: TrackInfo):
    global _current_session_track_id

    if _active_session_id is None:
        return

    # Already played — skip to next, but end session if too many skips in a row
    if info.track_id in _played_track_ids:
        log.info(f"Track {info.track_id} already played — skipping")
        try:
            sp = await get_spotify()
            await sp.playback_next()
        except Exception as e:
            log.error(f"Failed to skip already played track: {e}")
            # Don't end session — just log, session continues
        return

    _played_track_ids.add(info.track_id)

    # Remove voting buttons from previous track card
    if _current_session_track_id is not None and _current_session_track_id in _track_messages:
        for chat_id, message_id in _track_messages[_current_session_track_id]:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            except Exception:
                pass

    session_track_id = await create_session_track(_pool, _active_session_id, info)
    _current_session_track_id = session_track_id

    # Resolve added_by Spotify ID to Telegram name
    added_by_name = None
    if info.added_by:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_username, telegram_name FROM users WHERE spotify_id = $1", info.added_by
            )
            if row:
                added_by_name = f"@{row['telegram_username']}" if row["telegram_username"] else row["telegram_name"]

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

    track_display = format_track(info.title, info.artist, info.track_id)

    # Reserve space for vote result (~50 chars) in caption
    VOTE_RESULT_RESERVE = 50
    MAX_CAPTION = 1024 - VOTE_RESULT_RESERVE

    album_display = format_album(info.album)

    text = (
        f"🎵 {track_display}\n"
        f"💿 {album_display}"
        f"{added_by_text}{facts_text}"
    )

    # Trim facts by removing lines from bottom until it fits
    if len(text) > MAX_CAPTION and facts_text:
        header = (
            f"🎵 {track_display}\n"
            f"💿 {album_display}"
            f"{added_by_text}"
        )
        available = MAX_CAPTION - len(header) - 3  # 3 for \n\n💡 prefix
        if available > 30:
            fact_lines = facts.split("\n")
            trimmed = []
            total = 0
            for line in fact_lines:
                if total + len(line) + 1 <= available:
                    trimmed.append(line)
                    total += len(line) + 1
                else:
                    break
            facts_text = f"\n\n💡 " + "\n".join(trimmed) if trimmed else ""
        else:
            facts_text = ""
        text = (
            f"🎵 {track_display}\n"
            f"💿 {album_display}"
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


async def _finalize_track_card(session_track_id: int, result_text: str):
    """Update track card with result and remove voting buttons."""
    if session_track_id not in _track_messages:
        return

    for chat_id, message_id in _track_messages[session_track_id]:
        try:
            # Try to append result to caption (photo) or text (message)
            try:
                msg = await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id,
                    caption=None,  # will fail, we catch and just remove buttons
                    reply_markup=None,
                )
            except Exception:
                pass
            # Remove buttons
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass

    # Send result as reply
    for chat_id, message_id in _track_messages[session_track_id]:
        try:
            await bot.send_message(chat_id, result_text, parse_mode="HTML", reply_to_message_id=message_id)
        except Exception:
            pass


async def _check_session_complete():
    """Check if all tracks in session have been voted on — suggest ending."""
    if _active_session_id is None:
        return

    async with _pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM session_tracks WHERE session_id = $1 AND vote_result = 'pending'",
            _active_session_id,
        )

    if pending == 0:
        end_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏁 Завершить сессию", callback_data="confirm_end"),
            InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_session"),
        ]])
        await bot.send_message(
            settings.telegram_admin_id,
            "🎵 <b>Все треки прослушаны и оценены!</b>\n\nЗавершить сессию?",
            reply_markup=end_kb,
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "confirm_end")
async def on_confirm_end(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return
    await callback.answer("🏁 Завершаю...")
    await callback.message.edit_text("🏁 Сессия завершается...", parse_mode="HTML")
    await _end_session()


@dp.callback_query(F.data == "continue_session")
async def on_continue_session(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только ведущий!")
        return
    await callback.answer("▶️ Продолжаем!")
    await callback.message.edit_text("▶️ Продолжаем прослушивание!", parse_mode="HTML")


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

    # Only act when ALL participants have voted
    if result["total_votes"] < len(_participants):
        return

    if session_track_id in _skip_in_progress:
        return
    _skip_in_progress.add(session_track_id)

    # Determine final result text
    keep_count = result["total_votes"] - result["drop_count"]
    vote_result = result.get("vote_result") or ("drop" if result["drop_count"] >= result["threshold"] else "keep")
    emoji = "❌" if vote_result == "drop" else "✅"
    result_text = f"{keep_count} за / {result['drop_count']} против — {emoji} {vote_result}"

    # Finalize track card: result + remove buttons
    await _finalize_track_card(session_track_id, result_text)

    # If drop — remove from playlist
    if vote_result == "drop" and _active_playlist_id:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT spotify_track_id FROM session_tracks WHERE id = $1", session_track_id
            )
        if row:
            try:
                await remove_track_from_playlist(_active_playlist_id, row["spotify_track_id"])
            except Exception as e:
                log.error(f"Failed to remove track from playlist: {e}")

    # Skip to next track (only if this is the current track)
    if session_track_id == _current_session_track_id:
        try:
            await skip_to_next()
        except Exception as e:
            log.error(f"Failed to skip: {e}")

    # Check if all session tracks are voted — suggest end
    await _check_session_complete()


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
                "INSERT INTO users (telegram_id, telegram_name, telegram_username) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
                tid, "", "",
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

    # Update session message with participant list
    await _update_session_message()


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


@dp.message(Command("preview"))
async def cmd_preview(message: Message):
    """Preview a track card by name or Spotify URL. Admin only."""
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи ссылку: /preview https://open.spotify.com/track/...")
        return

    query = args[1].strip()
    track_id = _extract_track_id(query)
    if not track_id:
        await message.answer("Нужна ссылка на трек в Spotify.")
        return

    try:
        sp = await get_spotify()
        track = await sp.track(track_id)

        # Build card
        artist_str = ", ".join(a.name for a in track.artists)
        track_display = format_track(track.name, artist_str, track.id)
        cover_url = track.album.images[0].url if track.album.images else None

        text = (
            f"🎵 {track_display}\n"
            f"💿 {format_album(track.album.name)}\n"
            f"👤 preview mode\n\n"
            f"💡 Это превью карточки. В сессии будут кнопки Keep/Drop и AI-факты."
        )

        if cover_url:
            await message.answer_photo(photo=cover_url, caption=text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


def _extract_track_id(url_or_id: str) -> str | None:
    """Extract Spotify track ID from URL or URI."""
    match = re.search(r"track[/:]([a-zA-Z0-9]+)", url_or_id)
    if match:
        return match.group(1)
    if re.match(r"^[a-zA-Z0-9]{22}$", url_or_id):
        return url_or_id
    return None


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


# ---------------------------------------------------------------------------
# Admin commands: /distribute, /recap, /close_playlist, /create_next, /dbinfo
# ---------------------------------------------------------------------------

def _parse_turdom_number(text: str, command: str) -> int | None:
    """Extract TURDOM number from command arguments like '/distribute 91'."""
    args = text.split(maxsplit=1)
    if len(args) < 2:
        return None
    try:
        return int(args[1].strip())
    except ValueError:
        return None


@dp.message(Command("distribute"))
async def on_distribute(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    num = _parse_turdom_number(message.text, "distribute")
    if num is None:
        await message.answer("Укажи номер: /distribute 91")
        return

    result = await cmd_distribute(_pool, num, triggered_by=message.from_user.id)

    if result["status"] == "already_done":
        # Ask for confirmation with buttons
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Повторить", callback_data=f"redistribute:{num}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="redistribute:cancel"),
        ]])
        await message.answer(result["message"], reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(result["message"], parse_mode="HTML")


@dp.callback_query(F.data.startswith("redistribute:"))
async def on_redistribute(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ!")
        return

    action = callback.data.split(":")[1]
    if action == "cancel":
        await callback.answer("Ок")
        await callback.message.edit_text("⏭ Отменено.", parse_mode="HTML")
        return

    num = int(action)
    await callback.answer("Запускаю...")
    await callback.message.edit_text("⏳ Раскидываю треки...", parse_mode="HTML")
    result = await cmd_distribute_force(_pool, num, triggered_by=callback.from_user.id)
    await callback.message.edit_text(result["message"], parse_mode="HTML")


def _recap_keyboard(turdom_num: int, page: int, total: int) -> InlineKeyboardMarkup:
    """Build carousel keyboard for recap blocks."""
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="←", callback_data=f"recap_page:{turdom_num}:{page - 1}"))
    buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data="noop"))
    if page < total - 1:
        buttons.append(InlineKeyboardButton(text="→", callback_data=f"recap_page:{turdom_num}:{page + 1}"))
    rows = [buttons]
    rows.append([InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"rerecap:{turdom_num}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_recap_carousel(chat_id: int, recap_text: str, turdom_num: int):
    """Send first recap block as carousel with ← → navigation."""
    blocks = [b.strip() for b in recap_text.split("\n\n---\n\n") if b.strip()]
    if not blocks:
        return
    kb = _recap_keyboard(turdom_num, 0, len(blocks))
    await bot.send_message(chat_id, blocks[0], parse_mode="HTML", reply_markup=kb, link_preview_options=_NO_PREVIEW)


@dp.message(Command("recap"))
async def on_recap(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    num = _parse_turdom_number(message.text, "recap")
    if num is None:
        await message.answer("Укажи номер: /recap 91")
        return

    result = await cmd_recap(_pool, num, triggered_by=message.from_user.id)

    if result["status"] != "ok":
        await message.answer(result["message"], parse_mode="HTML")
        return

    recap_text = result.get("recap_text", result["message"])
    await _send_recap_carousel(message.chat.id, recap_text, num)


@dp.callback_query(F.data.startswith("recap_page:"))
async def on_recap_page(callback: CallbackQuery):
    """Carousel navigation for recap blocks."""
    parts = callback.data.split(":")
    turdom_num = int(parts[1])
    page = int(parts[2])

    # Get saved recap from DB
    async with _pool.acquire() as conn:
        pl = await conn.fetchrow("SELECT spotify_id FROM playlists WHERE number = $1", turdom_num)
        if pl:
            recap_text = await conn.fetchval(
                """SELECT recap_text FROM sessions
                   WHERE playlist_spotify_id = $1 ORDER BY id DESC LIMIT 1""",
                pl["spotify_id"],
            )

    if not recap_text:
        await callback.answer("Рекап не найден")
        return

    blocks = [b.strip() for b in recap_text.split("\n\n---\n\n") if b.strip()]
    if page < 0 or page >= len(blocks):
        await callback.answer("Нет такой страницы")
        return

    kb = _recap_keyboard(turdom_num, page, len(blocks))
    await callback.message.edit_text(blocks[page], parse_mode="HTML", reply_markup=kb, link_preview_options=_NO_PREVIEW)
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("rerecap:"))
async def on_rerecap(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ!")
        return

    num = int(callback.data.split(":")[1])
    await callback.answer("Генерирую...")
    await callback.message.edit_reply_markup(reply_markup=None)

    result = await cmd_recap_regenerate(_pool, num, triggered_by=callback.from_user.id)

    if result["status"] == "ok" and result.get("recap_text"):
        await _send_recap_carousel(callback.message.chat.id, result["recap_text"], num)
    else:
        await callback.message.answer(result["message"], parse_mode="HTML")


@dp.message(Command("close_playlist"))
async def on_close_playlist(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    num = _parse_turdom_number(message.text, "close_playlist")
    if num is None:
        await message.answer("Укажи номер: /close_playlist 91")
        return

    result = await cmd_close_playlist(_pool, num, triggered_by=message.from_user.id)
    await message.answer(result["message"], parse_mode="HTML")


@dp.message(Command("create_next"))
async def on_create_next(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    args = message.text.split(maxsplit=1)
    theme = args[1].strip() if len(args) > 1 else None

    result = await cmd_create_next(_pool, theme=theme, triggered_by=message.from_user.id)

    if result["status"] == "blocked":
        await message.answer(result["message"], parse_mode="HTML")
        return

    await message.answer(result["message"], parse_mode="HTML")

    # Notify participants from last session
    for tid in result.get("notify_ids", []):
        if tid != message.from_user.id:
            try:
                pl = result["playlist"]
                await bot.send_message(
                    tid,
                    f"🆕 <b>Новый плейлист:</b> {pl['name']}\n\nДобавляйте треки!\n{pl['url']}",
                    parse_mode="HTML",
                )
            except Exception:
                pass


@dp.message(Command("dbinfo"))
async def on_dbinfo(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    text = await cmd_dbinfo(_pool)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("backfill_genres"))
async def on_backfill_genres(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ.")
        return

    await message.answer("⏳ Запускаю бэкфилл жанров...")
    try:
        result = await backfill_genres(_pool)
        await message.answer(
            f"✅ Бэкфилл готов!\n"
            f"Обработано: {result['processed']}\n"
            f"Жанры найдены: {result['resolved']}"
        )
    except Exception as e:
        log.error(f"Genre backfill failed: {e}")
        await message.answer(f"❌ Ошибка: {e}")


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
                "SELECT telegram_id FROM session_participants WHERE session_id = $1 AND active = TRUE",
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

        track_fmt = format_track(track_title, artist, track_id)

        # Get playlist URL for where it was removed from
        async with pool.acquire() as conn:
            pl_row = await conn.fetchrow("SELECT url FROM playlists WHERE name = $1", playlist_name)
        removed_from = f"<a href=\"{pl_row['url']}\">{playlist_name}</a>" if pl_row and pl_row["url"] else playlist_name

        msg = f"🗑 <b>Дубликат удалён!</b>\n\n🎵 {track_fmt}\nУдалён из: {removed_from}\n\nУже был:\n{dup_text}"

        if telegram_id:
            try:
                await bot.send_message(telegram_id, msg, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
            except Exception:
                pass
        # Always notify admin too
        if telegram_id != settings.telegram_admin_id:
            try:
                await bot.send_message(settings.telegram_admin_id, msg, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
            except Exception:
                pass

    _on_duplicate_notify = on_duplicate_found
    watcher = DuplicateWatcher(pool, on_duplicate_found)
    asyncio.create_task(watcher.start())

    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)
