"""User-facing command handlers: /start, /reg, /next, /get, /genres, /check,
/stats, /mystats, /history, /join, /leave, /secret."""

import html
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.services.playlists import get_next_playlist, check_duplicate, get_track_isrc
from app.services.track_formatter import format_track, format_album
from app.services.ai import analyze_easter_egg
from app.bot.core import (
    bot, get_pool, is_admin, require_registered, extract_spotify_id,
    reply, send,
)
from app.bot.session_manager import session

router = Router()

HISTORY_PAGE_SIZE = 5

GENRE_EMOJIS = {
    "Electronic": "⚡", "Pop": "🎹", "Metal": "🤘", "Rock": "🎸",
    "Hip-Hop": "🎤", "Indie": "🎶", "DnB": "🥁", "R&B": "💜",
    "Chill": "🌊", "Soundtrack": "🎬", "Phonk": "👻",
}


# ── /start ──────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("history_"):
        session_id = args[1].replace("history_", "")
        if session_id.isdigit():
            await _show_session_details(message, int(session_id))
            return

    msg = (
        "🎵 <b>TURDOM Assistant</b>\n\n"
        "📋 <b>Команды:</b>\n"
        "/reg <code>spotify_url</code> — привязать Spotify\n"
        "/next — ссылка на следующий плейлист\n"
        "/get <code>номер</code> — ссылка на плейлист\n"
        "/join — присоединиться к сессии\n"
        "/leave — выйти из сессии\n"
        "/secret <code>текст</code> — оставить пасхалку\n"
        "/check <code>spotify_url</code> — проверить дубликат\n"
        "/stats — общая статистика\n"
        "/mystats — твоя статистика\n"
        "/history — история сессий\n"
        "/genres — жанровые плейлисты"
    )

    if is_admin(message.from_user.id):
        msg += (
            "\n\n🔧 <b>Админ:</b>\n"
            "/session start | end | kick <code>@user</code>\n"
            "/playlist create | close | status | link | reschedule\n"
            "/distribute <code>номер</code> — раскидать по жанрам\n"
            "/recap <code>номер</code> — рекап сессии\n"
            "/preview <code>spotify_url</code> — превью карточки\n"
            "/health — здоровье бота\n"
            "/backfill_genres — заполнить жанры\n"
            "/dbinfo — инфо о базе\n"
            "/import_all — импорт плейлистов\n"
            "/scan — скан дубликатов\n"
            "/auth — подключить Spotify"
        )

    await reply(message, msg)


# ── /reg ────────────────────────────────────────────────────────

@router.message(Command("reg"))
async def cmd_reg(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Регистрация доступна только через админа.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await reply(
            message,
            "Скинь ссылку на свой Spotify профиль:\n\n"
            "<code>/reg https://open.spotify.com/user/YOUR_ID</code>\n\n"
            "Найти можно: Spotify → твой профиль → Share → Copy link",
        )
        return

    spotify_id = extract_spotify_id(args[1].strip(), "user")
    if not spotify_id:
        await message.answer("Не могу распарсить Spotify ID. Скинь ссылку формата open.spotify.com/user/...")
        return

    tid = message.from_user.id
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, telegram_name, telegram_username, spotify_id, is_admin)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (telegram_id) DO UPDATE SET spotify_id = $4, telegram_name = $2, telegram_username = $3
            """,
            tid, message.from_user.full_name, message.from_user.username or "", spotify_id, is_admin(tid),
        )

    await reply(message, f"✅ Spotify привязан: <code>{spotify_id}</code>")


# ── /next ───────────────────────────────────────────────────────

@router.message(Command("next"))
@require_registered
async def cmd_next(message: Message):
    result = await get_next_playlist(get_pool())
    if result:
        link = result.get("invite_url") or result["url"]
        await reply(
            message,
            f"🎧 <b>{result['name']}</b> ({result['status']})\n\n{link}",
        )
    else:
        await message.answer("Нет предстоящих плейлистов в базе.")



# ── /get ────────────────────────────────────────────────────────

@router.message(Command("get"))
@require_registered
async def cmd_get(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи номер: /get 92")
        return

    try:
        num = int(args[1].strip())
    except ValueError:
        await message.answer("Укажи номер: /get 92")
        return

    async with get_pool().acquire() as conn:
        pl = await conn.fetchrow(
            "SELECT name, url, status FROM playlists WHERE number = $1", num
        )

    if not pl:
        await message.answer(f"TURDOM#{num} не найден.")
        return

    await reply(message, f"🎧 <b>{pl['name']}</b>\n\n{pl['url']}")


# ── /genres ─────────────────────────────────────────────────────

@router.message(Command("genres"))
@require_registered
async def cmd_genres(message: Message):
    from app.services.genre_distributor import _genre_playlist_ids, load_genre_playlist_ids

    if not _genre_playlist_ids:
        await load_genre_playlist_ids(get_pool())

    if not _genre_playlist_ids:
        await message.answer("Жанровые плейлисты не найдены.")
        return

    lines = ["🎸 <b>Жанровые плейлисты TURDOM</b>\n"]
    for name, spotify_id in sorted(_genre_playlist_ids.items()):
        short = name.replace("TURDOM ", "")
        emoji = GENRE_EMOJIS.get(short, "🎵")
        url = f"https://open.spotify.com/playlist/{spotify_id}"
        lines.append(f"{emoji} <a href=\"{url}\">{name}</a>")

    await reply(message, "\n".join(lines))


# ── /check ──────────────────────────────────────────────────────

@router.message(Command("check"))
@require_registered
async def cmd_check(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await reply(
            message,
            "Скинь ссылку на трек:\n<code>/check https://open.spotify.com/track/...</code>",
        )
        return

    track_id = extract_spotify_id(args[1].strip(), "track")
    if not track_id:
        track_id = args[1].strip()

    await message.answer("🔍 Проверяю...")

    # Fetch track info for fuzzy matching
    title, artist = None, None
    try:
        from app.spotify.auth import get_spotify
        sp = await get_spotify()
        track = await sp.track(track_id)
        title = track.name
        artist = ", ".join(a.name for a in track.artists)
    except Exception:
        pass

    isrc = await get_track_isrc(track_id)
    duplicates = await check_duplicate(get_pool(), track_id, isrc, title=title, artist=artist)

    if duplicates:
        match_labels = {
            "exact": "🎯 точное совпадение",
            "isrc": "🔗 тот же трек (другой альбом)",
            "fuzzy_exact": "🔍 совпадение после нормализации",
            "fuzzy_contains": "🔍 название содержится",
            "fuzzy_levenshtein": "🔍 похожее название",
        }
        lines = []
        for d in duplicates:
            label = match_labels.get(d["match"], d["match"])
            track_display = format_track(d["title"], d["artist"])
            lines.append(f"• {track_display}\n  {label} в {d['playlist']}\n  {d['url']}")
        await reply(
            message,
            f"⚠️ <b>Дубликат найден!</b>\n\n" + "\n\n".join(lines),
        )
    else:
        await message.answer("✅ Трек не найден в базе — можно добавлять!")


# ── /stats ──────────────────────────────────────────────────────

@router.message(Command("stats"))
@require_registered
async def cmd_stats(message: Message):
    from app.services.genre_distributor import classify_track

    async with get_pool().acquire() as conn:
        total_tracks = await conn.fetchval("SELECT COUNT(*) FROM playlist_tracks")
        total_playlists = await conn.fetchval("SELECT COUNT(*) FROM playlists WHERE number IS NOT NULL")
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE spotify_id IS NOT NULL")

        user_rows = await conn.fetch("""
            SELECT COALESCE(u.telegram_username, u.telegram_name) as name,
                   COUNT(pt.id) as tracks
            FROM users u
            LEFT JOIN playlist_tracks pt ON u.spotify_id = pt.added_by_spotify_id
            WHERE u.spotify_id IS NOT NULL
            GROUP BY name ORDER BY tracks DESC
        """)

        genre_rows = await conn.fetch("""
            SELECT genre, COUNT(*) as cnt
            FROM playlist_tracks
            WHERE genre IS NOT NULL AND genre <> ''
            GROUP BY genre ORDER BY cnt DESC
        """)

    genre_totals: dict[str, int] = {}
    for r in genre_rows:
        playlist = classify_track(r["genre"])
        if playlist:
            short = playlist.replace("TURDOM ", "")
            genre_totals[short] = genre_totals.get(short, 0) + r["cnt"]

    msg1 = (
        f"🎵 <b>TURDOM STATS</b>\n"
        f"<i>{total_tracks} треков · {total_playlists} сессий · {total_users} участников</i>\n\n"
        f"<b>📊 Жанровые плейлисты:</b>\n\n"
    )
    for name in ["Electronic", "Pop", "Metal", "Rock", "Hip-Hop", "Indie", "DnB", "R&B", "Chill", "Soundtrack", "Phonk"]:
        count = genre_totals.get(name, 0)
        emoji = GENRE_EMOJIS.get(name, "")
        msg1 += f"{emoji} {name} — {count}\n"

    async with get_pool().acquire() as conn:
        all_user_genres = await conn.fetch("""
            SELECT COALESCE(u.telegram_username, u.telegram_name) as name,
                   pt.genre, COUNT(*) as cnt
            FROM playlist_tracks pt
            JOIN users u ON u.spotify_id = pt.added_by_spotify_id
            WHERE pt.genre IS NOT NULL AND pt.genre <> ''
            GROUP BY name, pt.genre ORDER BY name, cnt DESC
        """)

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

    await reply(message, msg1)
    await reply(message, msg2)


# ── /mystats ────────────────────────────────────────────────────

@router.message(Command("mystats"))
@require_registered
async def cmd_mystats(message: Message):
    from app.services.genre_distributor import classify_track

    tid = message.from_user.id
    async with get_pool().acquire() as conn:
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

        sessions_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_participants WHERE telegram_id = $1", tid
        )

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

        genre_rows = await conn.fetch("""
            SELECT genre, COUNT(*) as cnt FROM playlist_tracks
            WHERE added_by_spotify_id = $1 AND genre IS NOT NULL AND genre <> ''
            GROUP BY genre ORDER BY cnt DESC
        """, spotify_id)

    genre_totals: dict[str, int] = {}
    for r in genre_rows:
        pl = classify_track(r["genre"])
        if pl:
            short = pl.replace("TURDOM ", "")
            genre_totals[short] = genre_totals.get(short, 0) + r["cnt"]

    top5 = sorted(genre_totals.items(), key=lambda x: -x[1])[:5]
    top_genre = top5[0][0] if top5 else "—"

    top_emoji = GENRE_EMOJIS.get(top_genre, "🎵")

    msg = (
        f"📊 <b>Статистика {display}</b>\n\n"
        f"🎵 Треков добавлено: <b>{total}</b>\n"
        f"📅 Сессий: <b>{sessions_count}</b>\n"
        f"✅ Осталось: <b>{votes_kept}</b> · ❌ Удалено: <b>{votes_dropped}</b>\n\n"
        f"<b>Топ жанры:</b>\n"
    )
    for g_name, g_count in top5:
        emoji = GENRE_EMOJIS.get(g_name, "")
        msg += f"{emoji} {g_name} — {g_count}\n"

    msg += f"\n{top_emoji} <b>Профиль: {top_genre} Lover</b>"

    await reply(message, msg)


# ── /history ────────────────────────────────────────────────────

@router.message(Command("history"))
@require_registered
async def cmd_history(message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) > 1 and args[1].strip().isdigit():
        session_num = int(args[1].strip())
        await _show_session_details(message, session_num)
        return

    await _show_history_page(message, offset=0)


async def _show_history_page(message_or_callback, offset: int):
    async with get_pool().acquire() as conn:
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

    buttons = []
    if offset > 0:
        buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"history:{offset - HISTORY_PAGE_SIZE}"))
    if offset + HISTORY_PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="▶️ Далее", callback_data=f"history:{offset + HISTORY_PAGE_SIZE}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    if hasattr(message_or_callback, 'answer'):
        await reply(message_or_callback, text, reply_markup=kb)
    else:
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def _show_session_details(message: Message, session_num: int):
    """Show detailed view of a specific session."""
    async with get_pool().acquire() as conn:
        sess = await conn.fetchrow("""
            SELECT s.id, s.playlist_name, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM session_participants WHERE session_id = s.id) as participants
            FROM sessions s WHERE s.id = $1
        """, session_num)

        if not sess:
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

    name = sess["playlist_name"]
    date = sess["started_at"].strftime("%d/%m/%Y %H:%M") if sess["started_at"] else "?"
    parts = sess["participants"]

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

    await reply(message, "\n".join(lines))


# ── /join ───────────────────────────────────────────────────────

@router.message(Command("join"))
@require_registered
async def cmd_join(message: Message):
    tid = message.from_user.id

    if is_admin(tid):
        if tid not in session.participants:
            session.participants.add(tid)
        if session.active_session_id:
            async with get_pool().acquire() as conn:
                await conn.execute(
                    "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    session.active_session_id, tid,
                )
        await message.answer("✅ Ты админ, ты всегда в деле!")
        return

    if session.active_session_id is None:
        await message.answer("Сейчас нет активной сессии. Подожди пока ведущий запустит /session")
        return

    if tid in session.participants:
        await message.answer("Ты уже в сессии!")
        return

    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"approve:{tid}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{tid}"),
    ]])
    await send(
        settings.telegram_admin_id,
        f"🙋 <b>{message.from_user.full_name}</b> (@{message.from_user.username or '—'}) хочет присоединиться. Пустить?",
        reply_markup=approve_kb,
    )
    await message.answer("⏳ Запрос отправлен ведущему. Жди подтверждения!")


# ── /leave ──────────────────────────────────────────────────────

@router.message(Command("leave"))
@require_registered
async def cmd_leave(message: Message):
    tid = message.from_user.id

    if tid not in session.participants:
        await message.answer("Ты не в сессии.")
        return

    session.participants.discard(tid)
    if session.active_session_id:
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE session_participants SET active = FALSE, left_at = NOW() WHERE session_id = $1 AND telegram_id = $2",
                session.active_session_id, tid,
            )
    await message.answer(f"👋 Ты вышел из сессии. Участников: {len(session.participants)}")


# ── /secret ─────────────────────────────────────────────────────

@router.message(Command("secret"))
@require_registered
async def cmd_secret(message: Message):
    if not session.active_session_id:
        await message.answer("Сессия ещё не запущена. Секрет можно оставить после /session.")
        return

    session_id = session.active_session_id

    async with get_pool().acquire() as conn:
        upcoming_pl = await conn.fetchrow(
            """SELECT p.id FROM playlists p
               JOIN sessions s ON s.playlist_spotify_id = p.spotify_id
               WHERE s.id = $1""",
            session_id,
        )

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        async with get_pool().acquire() as conn:
            current = await conn.fetchval(
                "SELECT secret_note FROM session_participants WHERE session_id = $1 AND telegram_id = $2",
                session_id, message.from_user.id,
            )

        msg = (
            "🥚 <b>Пасхалки TURDOM</b>\n\n"
            "Оставь секрет в своих треках! В конце сессии в рекапе раскроем все пасхалки "
            "и посмотрим, кто что заметил.\n\n"
            "<b>Примеры:</b>\n"
            "• «Все мои треки — из саундтреков к фильмам 90-х»\n"
            "• «Первые буквы моих треков складываются в слово»\n"
            "• «Добавил 3 трека одного артиста под разными именами»\n"
            "• «Каждый мой трек — из другой страны»\n"
            "• «Названия треков — это стенды из JoJo»\n\n"
            "Напиши: <code>/secret твой секрет</code>"
        )
        if current:
            msg += f"\n\n🔒 Твой текущий секрет: <i>{current}</i>"

        await reply(message, msg)
        return

    secret = args[1].strip()

    async with get_pool().acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM session_participants WHERE session_id = $1 AND telegram_id = $2",
            session_id, message.from_user.id,
        )
        if exists:
            await conn.execute(
                "UPDATE session_participants SET secret_note = $1 WHERE session_id = $2 AND telegram_id = $3",
                secret, session_id, message.from_user.id,
            )
        else:
            await conn.execute(
                "INSERT INTO session_participants (session_id, telegram_id, secret_note) VALUES ($1, $2, $3)",
                session_id, message.from_user.id, secret,
            )

    await reply(message, f"🥚 Секрет сохранён!\n🔒 <i>{html.escape(secret)}</i>\n\n⏳ Анализирую треки...")

    user_tracks = []
    if upcoming_pl:
        async with get_pool().acquire() as conn:
            spotify_id = await conn.fetchval(
                "SELECT spotify_id FROM users WHERE telegram_id = $1", message.from_user.id
            )
            if spotify_id:
                user_tracks = [dict(r) for r in await conn.fetch(
                    "SELECT title, artist FROM playlist_tracks WHERE playlist_id = $1 AND added_by_spotify_id = $2",
                    upcoming_pl["id"], spotify_id,
                )]

    if not user_tracks:
        await reply(message, "Не нашёл твоих треков в плейлисте — анализ будет в рекапе.")
        return

    analysis = await analyze_easter_egg(secret, user_tracks)
    if analysis:
        session.waiting_secret_clarification[message.from_user.id] = {
            "session_id": session_id,
            "secret": secret,
        }
        await reply(
            message,
            f"🔍 <b>Анализ пасхалки:</b>\n\n{analysis}\n\n"
            f"<i>Можешь уточнить или дополнить секрет — просто напиши текстом. "
            f"Или отправь /secret с новым описанием.</i>",
        )
    else:
        await reply(message, "Анализ не удался, но секрет сохранён — раскроем в рекапе!")


@router.message(lambda m: m.text and m.from_user.id in session.waiting_secret_clarification and not m.text.startswith("/"))
async def on_secret_clarification(message: Message):
    """Handle user's clarification for easter egg."""
    info = session.waiting_secret_clarification.pop(message.from_user.id)
    updated_secret = f"{info['secret']} | Уточнение: {message.text.strip()}"

    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE session_participants SET secret_note = $1 WHERE session_id = $2 AND telegram_id = $3",
            updated_secret, info["session_id"], message.from_user.id,
        )

    await reply(message, f"🥚 Секрет обновлён!\n🔒 <i>{html.escape(updated_secret)}</i>")
