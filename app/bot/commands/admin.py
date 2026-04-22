"""Admin command handlers: /auth, /session, /preview, /reschedule, /scan,
/import, /import_all, /kick, /end, /distribute, /recap, /close_playlist,
/create_next, /health, /dbinfo, /backfill_genres."""

import asyncio
import re
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.spotify.auth import start_oauth, exchange_code, run_oauth_callback_server, get_spotify
from app.services.playlists import (
    import_playlist, import_all_turdom, check_duplicate, get_track_isrc,
    create_next_playlist, reschedule_playlist,
)
from app.services.genre_resolver import backfill_genres
from app.services.ai import generate_track_facts
from app.services.track_formatter import build_track_caption
from app.services.admin_commands import (
    cmd_distribute, cmd_distribute_force, cmd_recap, cmd_recap_regenerate,
    cmd_close_playlist, cmd_create_next, cmd_dbinfo, log_action, check_duplicate_session,
)
from app.bot.core import (
    get_pool, is_admin, require_admin, extract_spotify_id, parse_turdom_number,
    reply, send, reply_photo,
)
from app.bot.session_manager import session

log = logging.getLogger(__name__)

router = Router()


# ── /auth ───────────────────────────────────────────────────────

@router.message(Command("auth"))
@require_admin
async def cmd_auth(message: Message):
    url = await start_oauth()
    await message.answer(f"Перейди по ссылке для авторизации:\n\n{url}")

    async def on_code(code: str):
        from app.spotify.auth import save_token_to_db
        token = await exchange_code(code)
        await save_token_to_db(get_pool(), token)
        await message.answer("✅ Spotify подключен!")

    asyncio.create_task(run_oauth_callback_server(on_code))


# ── /session ────────────────────────────────────────────────────

@router.message(Command("session"))
@require_admin
async def cmd_session(message: Message):
    args = message.text.split(maxsplit=1)
    subcommand = args[1].strip().lower() if len(args) > 1 else ""

    if subcommand == "end":
        if session.active_session_id is None:
            await message.answer("Нет активной сессии.")
            return
        await session.end_session()
        return

    if subcommand.startswith("kick"):
        await _handle_kick(message)
        return

    if subcommand and subcommand != "start":
        await message.answer("Команды: /session start, /session end, /session kick @user")
        return

    if session.active_session_id is not None:
        await message.answer("🚫 Сессия уже идёт! Сначала /session end")
        return

    # Find upcoming playlist
    async with get_pool().acquire() as conn:
        upcoming = await conn.fetchrow(
            "SELECT id, spotify_id, name FROM playlists WHERE status = 'upcoming' ORDER BY number DESC LIMIT 1"
        )

    if not upcoming:
        await message.answer("Нет upcoming плейлиста. Сначала /create_next")
        return

    pl_db_id = upcoming["id"]
    playlist_spotify_id = upcoming["spotify_id"]
    playlist_name = upcoming["name"]

    if await check_duplicate_session(get_pool(), playlist_spotify_id):
        await reply(message, "🚫 Для этого плейлиста уже есть сессия!")
        return

    # Auto-join admin
    tid = message.from_user.id
    if tid not in session.participants:
        session.participants.add(tid)
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (telegram_id, telegram_name, telegram_username, is_admin)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (telegram_id) DO UPDATE SET telegram_name = $2, telegram_username = $3
                """,
                tid, message.from_user.full_name, message.from_user.username or "", True,
            )

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO sessions (playlist_id, playlist_spotify_id, playlist_name) VALUES ($1, $2, $3) RETURNING id",
            pl_db_id, playlist_spotify_id, playlist_name,
        )
        session.active_session_id = row["id"]
        session.active_playlist_id = playlist_spotify_id
        session.played_track_ids = set()

        await conn.execute(
            "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            session.active_session_id, tid,
        )

    # Clear queue
    try:
        sp = await get_spotify()
        pl = await sp.playlist(playlist_spotify_id)
        playlist_name = pl.name
        await sp.playback_start_context(f"spotify:playlist:{playlist_spotify_id}")
        await asyncio.sleep(0.5)
        await sp.playback_pause()
        log.info(f"Queue cleared for playlist {playlist_name}")
    except Exception as e:
        log.warning(f"Failed to clear queue: {e}")
        playlist_name = playlist_spotify_id

    start_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
    ]])
    names = await session.get_participant_names()
    session_msg = await reply(
        message,
        f"🎧 Сессия создана: <b>{playlist_name}</b>\n"
        f"👥 Участников: {len(session.participants)} — {names}\n\n"
        f"Жди пока все присоединятся, потом жми кнопку.",
        reply_markup=start_kb,
    )
    session.session_message = (session_msg.chat.id, session_msg.message_id)


# ── /end (legacy) ──────────────────────────────────────────────

@router.message(Command("end"))
@require_admin
async def cmd_end(message: Message):
    if session.active_session_id is None:
        await message.answer("Нет активной сессии.")
        return
    await session.end_session()


# ── /kick (legacy) ─────────────────────────────────────────────

async def _handle_kick(message: Message):
    """Handle kick logic for /session kick and /kick."""
    parts = message.text.split()
    username = None
    for p in parts:
        if p.startswith("@"):
            username = p.lstrip("@")
            break
    if not username and len(parts) >= 2:
        username = parts[-1].lstrip("@")

    if not username or username in ("kick", "session"):
        await message.answer("Укажи @username: /session kick @username")
        return

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM users WHERE telegram_username = $1", username
        )

    if not row:
        await message.answer(f"Юзер @{username} не найден в базе.")
        return

    tid = row["telegram_id"]
    if tid not in session.participants:
        await message.answer(f"@{username} не в текущей сессии.")
        return

    session.participants.discard(tid)
    if session.active_session_id:
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE session_participants SET active = FALSE, left_at = NOW() WHERE session_id = $1 AND telegram_id = $2",
                session.active_session_id, tid,
            )
    await message.answer(f"👢 @{username} кикнут из сессии. Участников: {len(session.participants)}")
    try:
        await send(tid, "👢 Тебя убрали из текущей сессии.")
    except Exception as e:
        log.debug(f"Failed to notify {tid}: {e}")


@router.message(Command("kick"))
@require_admin
async def cmd_kick(message: Message):
    await _handle_kick(message)


# ── /preview ────────────────────────────────────────────────────

@router.message(Command("preview"))
@require_admin
async def cmd_preview(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи ссылку: /preview https://open.spotify.com/track/...")
        return

    track_id = extract_spotify_id(args[1].strip(), "track")
    if not track_id:
        await message.answer("Нужна ссылка на трек в Spotify.")
        return

    try:
        sp = await get_spotify()
        track = await sp.track(track_id)

        artist_str = ", ".join(a.name for a in track.artists)
        cover_url = track.album.images[0].url if track.album.images else None

        await message.answer("⏳ Генерирую факты...")
        release_date = track.album.release_date if hasattr(track.album, "release_date") else ""
        facts = await generate_track_facts(
            track.name, artist_str, track.album.name, release_date=release_date or ""
        )

        # Photo caption limit = 1024, plain text = 4096
        text = build_track_caption(
            track.name, artist_str, track.album.name, track.id, facts=facts,
            max_caption=1024 if cover_url else 4096,
        )

        if cover_url:
            await reply_photo(message, cover_url, text)
        else:
            await reply(message, text)

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")



# ── /scan ───────────────────────────────────────────────────────

@router.message(Command("scan"))
@require_admin
async def cmd_scan(message: Message):
    await message.answer("🔍 Сканирую upcoming плейлист на дубликаты...")
    try:
        async with get_pool().acquire() as conn:
            playlists = await conn.fetch(
                "SELECT id, spotify_id, name FROM playlists WHERE status IN ('active', 'upcoming')"
            )

        if not playlists:
            await message.answer("Нет active/upcoming плейлистов.")
            return

        sp = await get_spotify()
        removed_count = 0
        suspicious_count = 0

        for pl in playlists:
            all_items = []
            offset = 0
            while True:
                items = await sp.playlist_items(pl["spotify_id"], limit=100, offset=offset)
                all_items.extend(items.items)
                offset += len(items.items)
                if offset >= items.total:
                    break

            for item in all_items:
                if not item.track or not item.track.id:
                    continue
                track = item.track
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)
                if not isrc:
                    isrc = await get_track_isrc(track.id)

                duplicates = await check_duplicate(
                    get_pool(), track.id, isrc,
                    title=track.name, artist=", ".join(a.name for a in track.artists),
                )
                duplicates = [
                    d for d in duplicates
                    if d.get("playlist_id") != pl["id"] or d["match"].startswith("fuzzy_")
                ]

                if duplicates:
                    # Exact/ISRC → auto-remove, fuzzy → just report
                    has_exact = any(d["match"] in ("exact", "isrc") for d in duplicates)
                    fuzzy_only = [d for d in duplicates if d["match"].startswith("fuzzy_")]
                    if not has_exact:
                        # Fuzzy only — ask user to confirm
                        if fuzzy_only and _on_fuzzy_confirm:
                            suspicious_count += 1
                            await _on_fuzzy_confirm(
                                telegram_id=None,
                                track_title=track.name,
                                artist=", ".join(a.name for a in track.artists),
                                duplicates=fuzzy_only,
                                playlist_name=pl["name"],
                                track_id=track.id,
                                playlist_spotify_id=pl["spotify_id"],
                            )
                        continue
                    removed_count += 1
                    await sp.playlist_remove(pl["spotify_id"], [f"spotify:track:{track.id}"])
                    async with get_pool().acquire() as conn:
                        await conn.execute(
                            "DELETE FROM playlist_tracks WHERE playlist_id = $1 AND spotify_track_id = $2",
                            pl["id"], track.id,
                        )
                    if _on_duplicate_notify:
                        await _on_duplicate_notify(
                            telegram_id=None,
                            track_title=track.name,
                            artist=", ".join(a.name for a in track.artists),
                            duplicates=duplicates,
                            playlist_name=pl["name"],
                            track_id=track.id,
                        )

        parts = [f"✅ Сканирование завершено!"]
        if removed_count:
            parts.append(f"🗑 Удалено: {removed_count}")
        if suspicious_count:
            parts.append(f"🔍 На подтверждение: {suspicious_count}")
        if not removed_count and not suspicious_count:
            parts.append("Дубликатов не найдено")
        await message.answer(" ".join(parts))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ── /import_all ─────────────────────────────────────────────────

@router.message(Command("import_all"))
@require_admin
async def cmd_import_all(message: Message):
    await message.answer("⏳ Сканирую Spotify и импортирую все TURDOM плейлисты... Это займёт пару минут.")

    try:
        results = await import_all_turdom(get_pool())
        total_tracks = sum(r["tracks"] for r in results)
        text = f"✅ <b>Импорт завершён!</b>\n\nПлейлистов: {len(results)}\nТреков: {total_tracks}\n\n"
        for r in results[:20]:
            text += f"• {r['name']} — {r['tracks']} треков\n"
        if len(results) > 20:
            text += f"\n...и ещё {len(results) - 20}"
        await reply(message, text)
    except Exception as e:
        await message.answer(f"❌ Ошибка импорта: {e}")


# ── /import ─────────────────────────────────────────────────────

@router.message(Command("import"))
@require_admin
async def cmd_import(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи ссылку: /import &lt;playlist_url&gt;")
        return

    playlist_id = extract_spotify_id(args[1].strip(), "playlist")
    if not playlist_id:
        await message.answer("Не могу распарсить ID плейлиста.")
        return

    await message.answer("⏳ Импортирую...")
    try:
        result = await import_playlist(get_pool(), playlist_id)
        await reply(
            message,
            f"✅ <b>{result['name']}</b> — {result['tracks']} треков импортировано!",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ── /distribute ─────────────────────────────────────────────────

@router.message(Command("distribute"))
@require_admin
async def on_distribute(message: Message):
    num = parse_turdom_number(message.text)
    if num is None:
        await message.answer("Укажи номер: /distribute 91")
        return

    result = await cmd_distribute(get_pool(), num, triggered_by=message.from_user.id)
    if result["status"] == "already_done":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Повторить", callback_data=f"redistribute:{num}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="redistribute:cancel"),
        ]])
        await reply(message, result["message"], reply_markup=kb)
    else:
        await reply(message, result["message"])


# ── /recap ──────────────────────────────────────────────────────

@router.message(Command("recap"))
@require_admin
async def on_recap(message: Message):
    num = parse_turdom_number(message.text)
    if num is None:
        await message.answer("Укажи номер: /recap 91")
        return

    result = await cmd_recap(get_pool(), num, triggered_by=message.from_user.id)

    if result["status"] != "ok":
        await reply(message, result["message"])
        return

    recap_text = result.get("recap_text", result["message"])
    await session.send_recap_carousel(message.chat.id, recap_text, num)


# ── /playlist (create, close, status, link, reschedule) ────────

@router.message(Command("playlist"))
@require_admin
async def cmd_playlist(message: Message):
    args = message.text.split(maxsplit=2)
    sub = args[1].strip().lower() if len(args) > 1 else ""
    rest = args[2].strip() if len(args) > 2 else ""

    if sub == "create":
        theme = rest or None
        result = await cmd_create_next(get_pool(), theme=theme, triggered_by=message.from_user.id)

        if result["status"] == "blocked":
            await reply(message, result["message"])
            return

        await reply(message, result["message"])

        for tid in result.get("notify_ids", []):
            if tid != message.from_user.id:
                try:
                    pl = result["playlist"]
                    await send(tid, f"🆕 <b>Новый плейлист:</b> {pl['name']}\n\nДобавляйте треки!\n{pl['url']}")
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")

    elif sub == "close":
        if not rest or not rest.isdigit():
            await message.answer("Укажи номер: /playlist close 91")
            return
        result = await cmd_close_playlist(get_pool(), int(rest), triggered_by=message.from_user.id)
        await reply(message, result["message"])

    elif sub == "status":
        await _playlist_status(message)

    elif sub == "reschedule":
        if not rest or not re.match(r"\d{2}/\d{2}/\d{4}", rest):
            await message.answer("Формат: /playlist reschedule ДД/ММ/ГГГГ")
            return
        result = await reschedule_playlist(get_pool(), rest)
        if result:
            await reply(message, f"📅 Перенесено:\n<s>{result['old_name']}</s>\n→ <b>{result['new_name']}</b>")
        else:
            await message.answer("Нет предстоящих плейлистов для переноса.")

    else:
        await reply(
            message,
            "📋 <b>Команды плейлистов:</b>\n\n"
            "/playlist create <code>[тема]</code> — создать следующий\n"
            "/playlist close <code>номер</code> — закрыть плейлист\n"
            "/playlist status — статус плейлиста\n"
            "/playlist reschedule <code>ДД/ММ/ГГГГ</code> — перенести дату",
        )


async def _playlist_status(message: Message):
    """Show playlist readiness: tracks, facts, genres."""
    async with get_pool().acquire() as conn:
        pl = await conn.fetchrow(
            "SELECT id, name, number FROM playlists WHERE status IN ('active', 'upcoming') ORDER BY number DESC LIMIT 1"
        )

    if not pl:
        await message.answer("Нет active/upcoming плейлиста.")
        return

    async with get_pool().acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE t.ai_facts IS NOT NULL) as with_facts,
                   COUNT(*) FILTER (WHERE t.ai_facts IS NULL) as no_facts,
                   COUNT(*) FILTER (WHERE t.genre IS NOT NULL AND t.genre != 'unknown') as with_genre,
                   COUNT(*) FILTER (WHERE t.genre IS NULL OR t.genre = 'unknown') as no_genre
            FROM playlist_tracks pt JOIN tracks t ON pt.track_id = t.id
            WHERE pt.playlist_id = $1
        """, pl["id"])

        no_genre_tracks = await conn.fetch("""
            SELECT t.title, t.artist FROM playlist_tracks pt
            JOIN tracks t ON pt.track_id = t.id
            WHERE pt.playlist_id = $1 AND (t.genre IS NULL OR t.genre = 'unknown')
            ORDER BY t.title
        """, pl["id"])

        no_facts_tracks = await conn.fetch("""
            SELECT t.title, t.artist FROM playlist_tracks pt
            JOIN tracks t ON pt.track_id = t.id
            WHERE pt.playlist_id = $1 AND t.ai_facts IS NULL
            ORDER BY t.title
        """, pl["id"])

    lines = [f"📋 <b>{pl['name']}</b>\n"]
    lines.append(f"🎵 Треков: {stats['total']}")
    lines.append(f"💡 Факты: {stats['with_facts']}/{stats['total']}")
    lines.append(f"🎸 Жанры: {stats['with_genre']}/{stats['total']}")

    if stats["no_facts"] > 0:
        lines.append(f"\n❌ <b>Без фактов ({stats['no_facts']}):</b>")
        for t in no_facts_tracks[:10]:
            lines.append(f"   • {t['title']} — {t['artist']}")
        if stats["no_facts"] > 10:
            lines.append(f"   ...и ещё {stats['no_facts'] - 10}")

    if stats["no_genre"] > 0:
        lines.append(f"\n❌ <b>Без жанра ({stats['no_genre']}):</b>")
        for t in no_genre_tracks[:10]:
            lines.append(f"   • {t['title']} — {t['artist']}")
        if stats["no_genre"] > 10:
            lines.append(f"   ...и ещё {stats['no_genre'] - 10}")

    if stats["no_facts"] == 0 and stats["no_genre"] == 0:
        lines.append(f"\n✅ Всё готово к сессии!")

    await reply(message, "\n".join(lines))


# ── /health — bot health only ──────────────────────────────────

@router.message(Command("health"))
@require_admin
async def on_health(message: Message):
    import time
    from app.spotify.auth import get_spotify

    lines = ["🩺 <b>Health Check</b>\n"]

    # Uptime
    uptime_sec = int(time.time() - _bot_start_time)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, secs = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s"
    lines.append(f"🤖 Uptime: <b>{uptime_str}</b>")

    # Session
    if session.active_session_id:
        lines.append(
            f"🎧 Сессия #{session.active_session_id}: "
            f"{len(session.participants)} уч., "
            f"{len(session.played_track_ids)} треков"
        )
    else:
        lines.append("💤 Нет активной сессии")

    # Spotify
    try:
        sp = await get_spotify()
        playback = await sp.playback()
        if playback and playback.is_playing:
            lines.append("🟢 Spotify: играет")
        else:
            lines.append("⏸ Spotify: пауза")
    except Exception:
        lines.append("🔴 Spotify: недоступен")

    # DB
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        lines.append("🟢 PostgreSQL: ок")
    except Exception:
        lines.append("🔴 PostgreSQL: недоступен")

    await reply(message, "\n".join(lines))


_bot_start_time: float = 0


def init_health():
    """Call on bot startup to record start time."""
    import time
    global _bot_start_time
    _bot_start_time = time.time()


# ── /dbinfo ─────────────────────────────────────────────────────

@router.message(Command("dbinfo"))
@require_admin
async def on_dbinfo(message: Message):
    text = await cmd_dbinfo(get_pool())
    await reply(message, text)


# ── /backfill_genres ────────────────────────────────────────────

@router.message(Command("backfill_genres"))
@require_admin
async def on_backfill_genres(message: Message):
    args = message.text.split(maxsplit=1)
    reset = len(args) > 1 and args[1].strip().lower() == "reset"

    if reset:
        async with get_pool().acquire() as conn:
            await conn.execute("UPDATE tracks SET genre = NULL")
        await message.answer("🔄 Все жанры сброшены. Запускаю бэкфилл через Last.fm + AI...\n\n⚠️ Если бэкфилл прервётся — запусти /backfill_genres повторно.")
    else:
        await message.answer("⏳ Запускаю бэкфилл жанров (только пустые)...")

    try:
        result = await backfill_genres(get_pool())
        await reply(
            message,
            f"✅ Бэкфилл готов!\n"
            f"Обработано: {result['processed']}\n"
            f"Жанры найдены: {result['resolved']}\n"
            f"Не определено: {result['unknown']}",
        )
    except Exception as e:
        log.error(f"Genre backfill failed: {e}")
        await message.answer(f"❌ Ошибка: {e}")


# ── /backfill_normalized ────────────────────────────────────────

@router.message(Command("backfill_normalized"))
@require_admin
async def on_backfill_normalized(message: Message):
    """Backfill normalized_title, normalized_artist, normalized_base for all tracks."""
    from app.services.normalize import normalize_title, normalize_artist, base_title

    await message.answer("⏳ Запускаю нормализацию...")
    try:
        async with get_pool().acquire() as conn:
            tracks = await conn.fetch(
                """SELECT id, title, artist FROM tracks
                   WHERE normalized_title IS NULL
                      OR normalized_artist IS NULL
                      OR normalized_base IS NULL"""
            )
            updated = 0
            for t in tracks:
                await conn.execute(
                    """UPDATE tracks SET normalized_title = $1, normalized_artist = $2,
                                          normalized_base = $3 WHERE id = $4""",
                    normalize_title(t["title"]), normalize_artist(t["artist"]),
                    base_title(t["title"]), t["id"],
                )
                updated += 1

        await reply(message, f"✅ Нормализовано <b>{updated}</b> треков из {len(tracks)}")
    except Exception as e:
        log.error(f"Normalized backfill failed: {e}")
        await message.answer(f"❌ Ошибка: {e}")


# ── Duplicate notification helper (set in setup_bot) ───────────

_on_duplicate_notify = None
_on_fuzzy_confirm = None


def set_duplicate_notify(fn):
    global _on_duplicate_notify
    _on_duplicate_notify = fn


def set_fuzzy_confirm(fn):
    global _on_fuzzy_confirm
    _on_fuzzy_confirm = fn
