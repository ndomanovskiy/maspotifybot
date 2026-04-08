"""Admin commands: /distribute, /recap, /close_playlist, /create_next, /dbinfo.

All commands use TURDOM playlist number (e.g. 91) as identifier.
Actions are logged to action_log table.
Times displayed in MSK (UTC+3).
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import asyncpg

from app.services.ai import generate_session_recap
from app.services.genre_distributor import distribute_session_tracks
from app.services.playlists import create_next_playlist
from app.spotify.auth import get_spotify

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))


def to_msk(dt: datetime | None) -> str:
    """Format a UTC datetime as MSK string."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M MSK")


# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------

async def log_action(
    pool: asyncpg.Pool,
    action: str,
    *,
    turdom_number: int | None = None,
    session_id: int | None = None,
    playlist_id: int | None = None,
    triggered_by: int | None = None,
    params: dict | None = None,
    result: dict | None = None,
    status: str = "ok",
):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO action_log
               (action, turdom_number, session_id, playlist_id, triggered_by, params, result, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            action, turdom_number, session_id, playlist_id, triggered_by,
            json.dumps(params) if params else None,
            json.dumps(result) if result else None,
            status,
        )


# ---------------------------------------------------------------------------
# TURDOM# resolver
# ---------------------------------------------------------------------------

async def resolve_turdom(pool: asyncpg.Pool, turdom_number: int) -> dict | None:
    """Resolve TURDOM number to playlist + session info.

    Returns dict with keys: playlist_id, playlist_db_id, playlist_name,
    playlist_status, session_id, session_status, session_ended_at.
    Returns None if playlist not found.
    """
    async with pool.acquire() as conn:
        pl = await conn.fetchrow(
            "SELECT id, spotify_id, name, status FROM playlists WHERE number = $1",
            turdom_number,
        )
        if not pl:
            return None

        sess = await conn.fetchrow(
            """SELECT id, status, ended_at FROM sessions
               WHERE playlist_spotify_id = $1
               ORDER BY id DESC LIMIT 1""",
            pl["spotify_id"],
        )

        return {
            "playlist_db_id": pl["id"],
            "playlist_spotify_id": pl["spotify_id"],
            "playlist_name": pl["name"],
            "playlist_status": pl["status"],
            "session_id": sess["id"] if sess else None,
            "session_status": sess["status"] if sess else None,
            "session_ended_at": sess["ended_at"] if sess else None,
        }


async def check_duplicate_session(pool: asyncpg.Pool, playlist_spotify_id: str) -> bool:
    """Return True if a session already exists for this playlist."""
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM sessions WHERE playlist_spotify_id = $1",
            playlist_spotify_id,
        )
        return count > 0


# ---------------------------------------------------------------------------
# /distribute <turdom_number>
# ---------------------------------------------------------------------------

async def cmd_distribute(pool: asyncpg.Pool, turdom_number: int, triggered_by: int | None = None) -> dict:
    """Distribute kept tracks from a session to genre playlists.

    Returns dict: {status, message, distributed, skipped}.
    """
    info = await resolve_turdom(pool, turdom_number)
    if not info:
        return {"status": "error", "message": f"TURDOM#{turdom_number} не найден."}

    if not info["session_id"]:
        return {"status": "error", "message": f"Для TURDOM#{turdom_number} нет сессии."}

    if info["session_status"] != "ended":
        return {"status": "error", "message": f"Сессия TURDOM#{turdom_number} ещё активна, сначала заверши."}

    # Check if already distributed
    async with pool.acquire() as conn:
        distributed_at = await conn.fetchval(
            "SELECT distributed_at FROM sessions WHERE id = $1", info["session_id"]
        )

    if distributed_at:
        return {
            "status": "already_done",
            "message": (
                f"⚠️ Треки TURDOM#{turdom_number} уже раскиданы {to_msk(distributed_at)}.\n"
                f"Повторить?"
            ),
            "session_id": info["session_id"],
        }

    # Do the distribution
    result = await distribute_session_tracks(pool, info["session_id"])

    # Mark as distributed
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET distributed_at = NOW() WHERE id = $1", info["session_id"]
        )

    await log_action(
        pool, "distribute",
        turdom_number=turdom_number,
        session_id=info["session_id"],
        triggered_by=triggered_by,
        result=result,
    )

    return {
        "status": "ok",
        "message": f"🎶 Раскидал {result['distributed']} треков по жанровым плейлистам (пропущено: {result['skipped']}).",
        "distributed": result["distributed"],
        "skipped": result["skipped"],
    }


async def cmd_distribute_force(pool: asyncpg.Pool, turdom_number: int, triggered_by: int | None = None) -> dict:
    """Force re-distribute (after user confirmed)."""
    info = await resolve_turdom(pool, turdom_number)
    if not info or not info["session_id"]:
        return {"status": "error", "message": f"TURDOM#{turdom_number} не найден или нет сессии."}

    result = await distribute_session_tracks(pool, info["session_id"])

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET distributed_at = NOW() WHERE id = $1", info["session_id"]
        )

    await log_action(
        pool, "distribute_force",
        turdom_number=turdom_number,
        session_id=info["session_id"],
        triggered_by=triggered_by,
        result=result,
    )

    return {
        "status": "ok",
        "message": f"🎶 Повторно раскидал {result['distributed']} треков (пропущено: {result['skipped']}).",
        "distributed": result["distributed"],
        "skipped": result["skipped"],
    }


# ---------------------------------------------------------------------------
# /recap <turdom_number>
# ---------------------------------------------------------------------------

async def cmd_recap(pool: asyncpg.Pool, turdom_number: int, triggered_by: int | None = None) -> dict:
    """Get or generate recap for a session.

    Returns dict: {status, message, recap_text, has_saved, session_id}.
    """
    info = await resolve_turdom(pool, turdom_number)
    if not info:
        return {"status": "error", "message": f"TURDOM#{turdom_number} не найден."}

    if not info["session_id"]:
        return {"status": "error", "message": f"Для TURDOM#{turdom_number} нет сессии."}

    if info["session_status"] != "ended":
        return {"status": "error", "message": f"Сессия TURDOM#{turdom_number} ещё активна."}

    session_id = info["session_id"]

    # Check for saved recap
    async with pool.acquire() as conn:
        saved_recap = await conn.fetchval(
            "SELECT recap_text FROM sessions WHERE id = $1", session_id
        )

    if saved_recap:
        await log_action(
            pool, "recap_view",
            turdom_number=turdom_number, session_id=session_id,
            triggered_by=triggered_by,
        )
        return {
            "status": "ok",
            "message": f"📋 Сохранённый рекап TURDOM#{turdom_number}:\n\n{saved_recap}",
            "recap_text": saved_recap,
            "has_saved": True,
            "session_id": session_id,
        }

    # Generate new recap
    recap_text = await _generate_and_save_recap(pool, session_id, turdom_number, triggered_by)

    return {
        "status": "ok",
        "message": f"🤖 AI Рекап TURDOM#{turdom_number}:\n\n{recap_text}" if recap_text else "❌ Не удалось сгенерировать рекап.",
        "recap_text": recap_text,
        "has_saved": False,
        "session_id": session_id,
    }


async def cmd_recap_regenerate(pool: asyncpg.Pool, turdom_number: int, triggered_by: int | None = None) -> dict:
    """Force regenerate recap."""
    info = await resolve_turdom(pool, turdom_number)
    if not info or not info["session_id"]:
        return {"status": "error", "message": f"TURDOM#{turdom_number} не найден или нет сессии."}

    recap_text = await _generate_and_save_recap(pool, info["session_id"], turdom_number, triggered_by)

    return {
        "status": "ok",
        "message": f"🔄 Перегенерированный рекап TURDOM#{turdom_number}:\n\n{recap_text}" if recap_text else "❌ Не удалось сгенерировать рекап.",
        "recap_text": recap_text,
    }


async def _generate_and_save_recap(
    pool: asyncpg.Pool, session_id: int, turdom_number: int, triggered_by: int | None
) -> str:
    """Generate structured recap with stats + AI commentary."""
    from app.services.genre_distributor import classify_track

    async with pool.acquire() as conn:
        # Tracks in listening order with genres and facts
        tracks_data = [dict(r) for r in await conn.fetch(
            """SELECT st.id, st.title, st.artist, st.vote_result, st.added_by_spotify_id,
                      COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name, st.added_by_spotify_id, '?') as added_by,
                      pt.genre, pt.ai_facts
               FROM session_tracks st
               LEFT JOIN users u ON st.added_by_spotify_id = u.spotify_id
               LEFT JOIN playlist_tracks pt ON st.spotify_track_id = pt.spotify_track_id
               WHERE st.session_id = $1
               ORDER BY st.created_at""",
            session_id,
        )]

        # Vote details: who voted drop on which track
        drop_votes = await conn.fetch(
            """SELECT v.session_track_id, v.telegram_id,
                      COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name, '?') as voter_name
               FROM votes v
               JOIN users u ON v.telegram_id = u.telegram_id
               WHERE v.session_track_id IN (
                   SELECT id FROM session_tracks WHERE session_id = $1
               ) AND v.vote = 'drop'""",
            session_id,
        )

        participant_names = [r["name"] for r in await conn.fetch(
            """SELECT COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name) as name
               FROM session_participants sp
               JOIN users u ON sp.telegram_id = u.telegram_id
               WHERE sp.session_id = $1""",
            session_id,
        )]

    total = len(tracks_data)
    kept = sum(1 for t in tracks_data if t["vote_result"] == "keep")
    dropped = total - kept

    # --- Per-user stats ---
    user_stats: dict[str, dict] = {}
    for t in tracks_data:
        name = t["added_by"]
        if name not in user_stats:
            user_stats[name] = {"kept": 0, "total": 0}
        user_stats[name]["total"] += 1
        if t["vote_result"] == "keep":
            user_stats[name]["kept"] += 1

    # --- Genre mix ---
    genre_counts: dict[str, int] = {}
    for t in tracks_data:
        genre = t.get("genre")
        if genre and genre != "unknown":
            playlist = classify_track(genre)
            if playlist:
                short = playlist.replace("TURDOM ", "")
                genre_counts[short] = genre_counts.get(short, 0) + 1

    # --- Mimic (best survival %) ---
    mimic = None
    best_rate = -1
    for name, s in user_stats.items():
        if s["total"] >= 2:  # at least 2 tracks to be meaningful
            rate = s["kept"] / s["total"]
            if rate > best_rate:
                best_rate = rate
                mimic = name

    # --- Rebel (most drops) ---
    rebel = None
    max_drops = 0
    for name, s in user_stats.items():
        drops = s["total"] - s["kept"]
        if drops > max_drops:
            max_drops = drops
            rebel = name

    # --- Killers (who voted drop on rebel's tracks most) ---
    killers: list[str] = []
    if rebel:
        # Find rebel's dropped track IDs
        rebel_dropped_ids = {t["id"] for t in tracks_data
                            if t["added_by"] == rebel and t["vote_result"] == "drop"}
        # Count who voted drop on those
        killer_counts: dict[str, int] = {}
        for v in drop_votes:
            if v["session_track_id"] in rebel_dropped_ids:
                killer_counts[v["voter_name"]] = killer_counts.get(v["voter_name"], 0) + 1
        if killer_counts:
            max_kills = max(killer_counts.values())
            killers = [name for name, cnt in killer_counts.items() if cnt == max_kills]

    # === BUILD STATS BLOCK ===
    lines = [f"📊 <b>TURDOM#{turdom_number} — Рекап</b>\n"]
    lines.append(f"🎵 {kept} остался{'ось' if kept != 1 else ''}, {dropped} удалили из {total}\n")

    lines.append("👤 <b>Статистика:</b>")
    for name, s in sorted(user_stats.items(), key=lambda x: -x[1]["kept"]):
        lines.append(f"   {name} — {s['kept']} из {s['total']}")

    if genre_counts:
        lines.append(f"\n⚡ <b>Жанровый микс:</b>")
        genre_str = " · ".join(f"{g} {c}" for g, c in sorted(genre_counts.items(), key=lambda x: -x[1]))
        lines.append(f"   {genre_str}")

    stats_block = "\n".join(lines)

    # === AI COMMENTARY ===
    # Build context for AI
    tracks_for_ai = ""
    for i, t in enumerate(tracks_data, 1):
        status = "✅" if t["vote_result"] == "keep" else "❌"
        genre = t.get("genre")
        genre_info = f" [{genre}]" if genre and genre != "unknown" else ""
        facts_info = f" Факты: {t.get('ai_facts')}" if t.get("ai_facts") else ""
        tracks_for_ai += f"{i}. {status} {t['title']} — {t['artist']} (от {t['added_by']}){genre_info}{facts_info}\n"

    mimic_info = f"Мимик: {mimic} ({user_stats[mimic]['kept']}/{user_stats[mimic]['total']})" if mimic else "нет"
    rebel_info = f"Бунтарь: {rebel} ({max_drops} дропов)" if rebel else "нет"
    killers_info = f"Киллеры: {', '.join(killers)}" if killers else "нет"

    ai_comment = await generate_session_recap(
        total, kept, dropped,
        tracks_for_ai, participant_names,
        mimic_info, rebel_info, killers_info,
    )

    # === COMBINE ===
    if ai_comment:
        recap_text = f"{stats_block}\n\n{ai_comment}"
    else:
        recap_text = stats_block

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET recap_text = $1 WHERE id = $2",
            recap_text, session_id,
        )

    await log_action(
        pool, "recap_generate",
        turdom_number=turdom_number, session_id=session_id,
        triggered_by=triggered_by,
        result={"length": len(recap_text)} if recap_text else {"error": "generation_failed"},
    )

    return recap_text


# ---------------------------------------------------------------------------
# /close_playlist <turdom_number>
# ---------------------------------------------------------------------------

async def cmd_close_playlist(pool: asyncpg.Pool, turdom_number: int, triggered_by: int | None = None) -> dict:
    """Close playlist: set status='listened', update date to actual session date."""
    info = await resolve_turdom(pool, turdom_number)
    if not info:
        return {"status": "error", "message": f"TURDOM#{turdom_number} не найден."}

    if info["playlist_status"] == "listened":
        return {"status": "error", "message": f"TURDOM#{turdom_number} уже закрыт."}

    # Determine actual date: from session ended_at or NOW
    actual_date = info["session_ended_at"] or datetime.now(timezone.utc)
    if actual_date.tzinfo is None:
        actual_date = actual_date.replace(tzinfo=timezone.utc)
    date_str = actual_date.astimezone(MSK).strftime("%d/%m/%Y")

    # Update playlist name with actual date
    old_name = info["playlist_name"]
    new_name = re.sub(r"\d{2}/\d{2}/\d{4}", date_str, old_name)
    if new_name == old_name:
        # No date in name, append it
        new_name = f"{old_name} {date_str}"

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE playlists SET status = 'listened', name = $1 WHERE id = $2",
            new_name, info["playlist_db_id"],
        )

    # Also rename in Spotify
    try:
        sp = await get_spotify()
        await sp.playlist_change_details(info["playlist_spotify_id"], name=new_name)
    except Exception as e:
        log.error(f"Failed to rename playlist in Spotify: {e}")

    await log_action(
        pool, "close_playlist",
        turdom_number=turdom_number,
        playlist_id=info["playlist_db_id"],
        triggered_by=triggered_by,
        result={"old_name": old_name, "new_name": new_name, "actual_date": date_str},
    )

    msg = f"✅ TURDOM#{turdom_number} закрыт.\n"
    if old_name != new_name:
        msg += f"📅 Переименован: {old_name} → {new_name}"
    else:
        msg += f"📅 Дата уже актуальна: {new_name}"

    return {"status": "ok", "message": msg}


# ---------------------------------------------------------------------------
# /create_next [theme]
# ---------------------------------------------------------------------------

async def cmd_create_next(pool: asyncpg.Pool, theme: str | None = None, triggered_by: int | None = None) -> dict:
    """Create next TURDOM playlist. Hard-blocks if open playlist exists."""
    async with pool.acquire() as conn:
        open_pl = await conn.fetchrow(
            "SELECT name, number, status FROM playlists WHERE status IN ('active', 'upcoming') ORDER BY number DESC LIMIT 1"
        )

    if open_pl:
        await log_action(
            pool, "create_next",
            triggered_by=triggered_by,
            status="blocked",
            result={"reason": "open_playlist", "name": open_pl["name"]},
        )
        return {
            "status": "blocked",
            "message": (
                f"🚫 Есть открытый плейлист: {open_pl['name']} ({open_pl['status']}).\n"
                f"Сначала закрой через /close_playlist {open_pl['number']}"
            ),
        }

    result = await create_next_playlist(pool, theme=theme)

    # Get participants from last ended session to notify them
    async with pool.acquire() as conn:
        last_session = await conn.fetchrow(
            "SELECT id FROM sessions WHERE status = 'ended' ORDER BY ended_at DESC LIMIT 1"
        )
        participant_ids = []
        if last_session:
            rows = await conn.fetch(
                "SELECT telegram_id FROM session_participants WHERE session_id = $1 AND active = true",
                last_session["id"],
            )
            participant_ids = [r["telegram_id"] for r in rows]

    await log_action(
        pool, "create_next",
        turdom_number=result["number"],
        triggered_by=triggered_by,
        result={"name": result["name"], "url": result["url"]},
    )

    return {
        "status": "ok",
        "message": (
            f"✅ Создан: {result['name']}\n\n"
            f"{result['url']}\n\n"
            f"📎 Открой плейлист в Spotify → Invite Collaborators → скинь ссылку:\n"
            f"/setnextlink &lt;ссылка&gt;"
        ),
        "playlist": result,
        "notify_ids": participant_ids,
    }


# ---------------------------------------------------------------------------
# /dbinfo
# ---------------------------------------------------------------------------

async def cmd_dbinfo(pool: asyncpg.Pool) -> str:
    """Return a compact overview of recent data."""
    async with pool.acquire() as conn:
        # Last 3 sessions
        sessions = await conn.fetch("""
            SELECT s.id, s.playlist_name, s.status, s.started_at, s.ended_at,
                   s.distributed_at, s.recap_text IS NOT NULL as has_recap,
                   COUNT(st.id) as total_tracks,
                   COUNT(st.id) FILTER (WHERE st.vote_result = 'keep') as kept,
                   COUNT(st.id) FILTER (WHERE st.vote_result = 'drop') as dropped
            FROM sessions s
            LEFT JOIN session_tracks st ON st.session_id = s.id
            GROUP BY s.id ORDER BY s.id DESC LIMIT 3
        """)

        # Open playlists
        open_pls = await conn.fetch(
            "SELECT name, number, status FROM playlists WHERE status IN ('active', 'upcoming') ORDER BY number DESC"
        )

        # User count
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users")

        # Last 5 actions
        actions = await conn.fetch(
            "SELECT action, turdom_number, status, created_at FROM action_log ORDER BY id DESC LIMIT 5"
        )

    lines = ["📊 <b>Database Info</b>\n"]

    # Sessions
    lines.append("<b>Последние сессии:</b>")
    for s in sessions:
        dist = "✅" if s["distributed_at"] else "❌"
        recap = "✅" if s["has_recap"] else "❌"
        lines.append(
            f"  #{s['id']} {s['playlist_name']} — {s['status']}\n"
            f"    📅 {to_msk(s['started_at'])} → {to_msk(s['ended_at'])}\n"
            f"    🎵 {s['total_tracks']} треков ({s['kept']} kept / {s['dropped']} dropped)\n"
            f"    Distribute: {dist} | Recap: {recap}"
        )

    # Open playlists
    if open_pls:
        lines.append("\n<b>Открытые плейлисты:</b>")
        for pl in open_pls:
            lines.append(f"  🟡 {pl['name']} ({pl['status']})")
    else:
        lines.append("\n<b>Открытые плейлисты:</b> нет")

    lines.append(f"\n<b>Юзеров:</b> {user_count}")

    # Action log
    if actions:
        lines.append("\n<b>Последние действия:</b>")
        for a in actions:
            num = f" #{a['turdom_number']}" if a["turdom_number"] else ""
            lines.append(f"  {a['action']}{num} — {a['status']} — {to_msk(a['created_at'])}")
    else:
        lines.append("\n<b>Лог действий:</b> пусто")

    return "\n".join(lines)
