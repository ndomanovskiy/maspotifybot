"""All callback_query handlers: voting, session control, history pagination,
recap carousel, playlist creation, join approval."""

import asyncio
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.spotify.auth import get_spotify
from app.services.voting import record_vote, remove_track_from_playlist, skip_to_next
from app.services.playlists import create_next_playlist
from app.services.ai import generate_track_facts
from app.services.track_formatter import format_track
from app.services.admin_commands import cmd_distribute_force, cmd_recap_regenerate, log_action
from app.bot.core import (
    get_pool, is_admin, is_registered,
    require_admin_callback, send, safe_int, _NO_PREVIEW,
)
from app.bot.session_manager import session

log = logging.getLogger(__name__)

router = Router()


# ── Start listening ─────────────────────────────────────────────

@router.callback_query(F.data == "start_listening")
@require_admin_callback
async def on_start_listening(callback: CallbackQuery):
    if session.active_playlist_id is None:
        await callback.answer("Нет активной сессии!")
        return

    try:
        sp = await get_spotify()
        devices = await sp.playback_devices()
        if not devices:
            await callback.answer("❌ Открой Spotify на устройстве и нажми ещё раз!", show_alert=True)
            return
        device_id = devices[0].id
        await sp.playback_shuffle(True, device_id=device_id)
        await sp.playback_start_context(f"spotify:playlist:{session.active_playlist_id}", device_id=device_id)
    except Exception as e:
        log.error(f"Failed to start playback: {e}")
        await callback.answer(f"Ошибка Spotify: {e}", show_alert=True)
        return

    session.monitor.on_track_change(lambda info: asyncio.ensure_future(session.on_track_change(info)))

    asyncio.create_task(session.cache_pre_recap_teaser())

    await callback.answer("▶️ Поехали!")
    await callback.message.edit_text(
        f"▶️ <b>Прослушивание запущено!</b> Участников: {len(session.participants)}",
        parse_mode="HTML",
    )

    for tid in session.participants:
        if tid != callback.from_user.id:
            try:
                await send(tid, "▶️ <b>Прослушивание началось!</b> Голосуй за треки!")
            except Exception as e:
                log.debug(f"Failed to notify {tid}: {e}")

    asyncio.create_task(session.monitor.start(session.active_playlist_id))


# ── Session end/continue ───────────────────────────────────────

@router.callback_query(F.data == "confirm_end")
@require_admin_callback
async def on_confirm_end(callback: CallbackQuery):
    await callback.answer("🏁 Завершаю...")
    await callback.message.edit_text("🏁 Сессия завершается...", parse_mode="HTML")
    await session.end_session()


@router.callback_query(F.data == "continue_session")
@require_admin_callback
async def on_continue_session(callback: CallbackQuery):
    session.session_end_prompted = False
    await callback.answer("▶️ Продолжаем!")
    await callback.message.edit_text("▶️ Продолжаем прослушивание!", parse_mode="HTML")


# ── Voting ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery):
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    vote_type = parts[1]
    session_track_id = safe_int(parts[2])
    if session_track_id is None:
        await callback.answer("Ошибка")
        return

    result = await record_vote(get_pool(), session_track_id, callback.from_user.id, vote_type, session_id=session.active_session_id)

    if result["status"] == "already_voted":
        await callback.answer("Ты уже голосовал за этот трек!")
        return

    if result["status"] == "vote_changed":
        emoji = "✅" if vote_type == "keep" else "❌"
        await callback.answer(f"{emoji} Голос изменён!")
    else:
        emoji = "✅" if vote_type == "keep" else "❌"
        await callback.answer(f"{emoji} Голос засчитан!")

    await session.update_vote_buttons(session_track_id)

    if result["total_votes"] < result.get("participants", len(session.participants)):
        return

    if session_track_id in session.skip_in_progress:
        return
    session.skip_in_progress.add(session_track_id)

    keep_count = result["total_votes"] - result["drop_count"]
    vote_result = result.get("vote_result") or ("drop" if result["drop_count"] >= result["threshold"] else "keep")
    emoji = "❌" if vote_result == "drop" else "✅"
    result_text = f"{keep_count} за / {result['drop_count']} против — {emoji} {vote_result}"

    await session.finalize_track_card(session_track_id, result_text)

    if vote_result == "drop" and session.active_playlist_id:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.spotify_track_id FROM session_tracks st JOIN tracks t ON st.track_id = t.id WHERE st.id = $1", session_track_id
            )
        if row:
            try:
                await remove_track_from_playlist(session.active_playlist_id, row["spotify_track_id"])
            except Exception as e:
                log.error(f"Failed to remove track from playlist: {e}")

    if session_track_id == session.current_session_track_id:
        try:
            await skip_to_next()
        except Exception as e:
            log.error(f"Failed to skip: {e}")

    await session.check_session_complete()


# ── Skip ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("skip:"))
@require_admin_callback
async def on_skip(callback: CallbackQuery):
    await callback.answer("⏭ Скипаю...")
    await skip_to_next()


@router.callback_query(F.data.startswith("skip_ext:"))
@require_admin_callback
async def on_skip_external(callback: CallbackQuery):
    """Skip a track that is not in the playlist."""
    await callback.answer("⏭ Скипаю...")
    await skip_to_next()


# ── Fire reaction 🔥 ───────────────────────────────────────────

@router.callback_query(F.data.startswith("fire:"))
async def on_fire(callback: CallbackQuery):
    """Record 🔥 reaction on a track."""
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return

    session_track_id = safe_int(callback.data.split(":")[1])
    if session_track_id is None:
        await callback.answer("Ошибка")
        return

    async with get_pool().acquire() as conn:
        # Toggle: insert or delete
        existing = await conn.fetchval(
            "SELECT id FROM track_reactions WHERE session_track_id = $1 AND telegram_id = $2",
            session_track_id, callback.from_user.id,
        )
        if existing:
            await conn.execute(
                "DELETE FROM track_reactions WHERE id = $1", existing,
            )
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM track_reactions WHERE session_track_id = $1", session_track_id,
            )
            await callback.answer(f"🔥 убрано ({count})")
        else:
            await conn.execute(
                "INSERT INTO track_reactions (session_track_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                session_track_id, callback.from_user.id,
            )
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM track_reactions WHERE session_track_id = $1", session_track_id,
            )
            await callback.answer(f"🔥 ({count})")


# ── Regen facts ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("regen_facts:"))
@require_admin_callback
async def on_regen_facts(callback: CallbackQuery):
    session_track_id = safe_int(callback.data.split(":")[1])
    if session_track_id is None:
        await callback.answer("Ошибка")
        return
    await callback.answer("🔄 Генерирую новые факты...")

    async with get_pool().acquire() as conn:
        track = await conn.fetchrow(
            "SELECT t.spotify_track_id, t.title, t.artist, t.album FROM session_tracks st JOIN tracks t ON st.track_id = t.id WHERE st.id = $1",
            session_track_id,
        )

    if not track:
        return

    # Fetch release_date from Spotify for date hint
    release_date = ""
    try:
        from app.spotify.auth import get_spotify
        sp = await get_spotify()
        sp_track = await sp.track(track["spotify_track_id"])
        release_date = sp_track.album.release_date if hasattr(sp_track.album, "release_date") else ""
    except Exception:
        pass

    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE tracks SET ai_facts = NULL WHERE spotify_track_id = $1",
            track["spotify_track_id"],
        )

    facts = await generate_track_facts(track["title"], track["artist"], track["album"] or "", release_date=release_date or "")

    if facts:
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE tracks SET ai_facts = $1 WHERE spotify_track_id = $2",
                facts, track["spotify_track_id"],
            )

        if session_track_id in session.track_messages:
            for chat_id, message_id, _ in session.track_messages[session_track_id]:
                try:
                    await send(chat_id, f"💡 <b>Обновлённые факты:</b>\n\n{facts}")
                except Exception as e:
                    log.debug(f"Failed to notify {chat_id}: {e}")
    else:
        await callback.message.answer("❌ Не удалось сгенерировать факты.", parse_mode="HTML")


# ── Approve/Deny join ──────────────────────────────────────────

@router.callback_query(F.data.startswith("approve:"))
@require_admin_callback
async def on_approve(callback: CallbackQuery):
    tid = safe_int(callback.data.split(":")[1])
    if tid is None:
        await callback.answer("Ошибка")
        return
    if tid not in session.participants:
        session.participants.add(tid)
        async with get_pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, telegram_name, telegram_username) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
                tid, "", "",
            )
            if session.active_session_id:
                await conn.execute(
                    "INSERT INTO session_participants (session_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    session.active_session_id, tid,
                )

    await callback.answer("✅ Одобрено!")
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ Одобрено!", parse_mode="HTML")
    try:
        await send(tid, f"✅ Ты в деле! Участников: {len(session.participants)}")
    except Exception as e:
        log.debug(f"Failed to notify {tid}: {e}")

    await session.update_session_message()


@router.callback_query(F.data.startswith("deny:"))
@require_admin_callback
async def on_deny(callback: CallbackQuery):
    tid = safe_int(callback.data.split(":")[1])
    if tid is None:
        await callback.answer("Ошибка")
        return
    await callback.answer("❌ Отказано")
    await callback.message.edit_text(f"{callback.message.text}\n\n❌ Отказано", parse_mode="HTML")
    try:
        await send(tid, "❌ Ведущий не одобрил присоединение.")
    except Exception as e:
        log.debug(f"Failed to notify {tid}: {e}")


@router.callback_query(F.data == "join_session")
async def on_join_session(callback: CallbackQuery):
    tid = callback.from_user.id

    if tid in session.participants:
        await callback.answer("Ты уже в сессии!")
        return

    if session.active_session_id is None:
        await callback.answer("Сессия уже закончилась!")
        return

    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"approve:{tid}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{tid}"),
    ]])
    await send(
        settings.telegram_admin_id,
        f"🙋 <b>{callback.from_user.full_name}</b> (@{callback.from_user.username or '—'}) хочет присоединиться. Пустить?",
        reply_markup=approve_kb,
    )
    await callback.answer("⏳ Запрос отправлен ведущему!")
    await callback.message.edit_text(
        f"{callback.message.text}\n\n⏳ Запрос отправлен, жди подтверждения...",
        parse_mode="HTML",
    )


# ── Create playlist ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("create_playlist:"))
@require_admin_callback
async def on_create_playlist(callback: CallbackQuery):
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
        session.waiting_theme = True
        return

    # Normal playlist
    await callback.answer("Создаю...")
    try:
        result = await create_next_playlist(get_pool())

        # Log auto-closed playlists
        for closed in result.get("auto_closed", []):
            await log_action(
                get_pool(), "auto_close_playlist",
                turdom_number=closed["number"],
                playlist_id=closed["id"],
                triggered_by=callback.from_user.id,
                result={"name": closed["name"], "reason": "create_next_callback"},
            )

        text = (
            f"✅ <b>Создан: {result['name']}</b>\n\n{result['url']}"
        )
        await callback.message.edit_text(text, parse_mode="HTML")

        for tid in session.participants:
            if tid != callback.from_user.id:
                try:
                    await send(tid, f"🆕 <b>Новый плейлист:</b> {result['name']}\n\nДобавляйте треки!\n{result['url']}")
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")


# ── Theme input ─────────────────────────────────────────────────
# Note: this is registered on dp directly in __init__.py since it uses
# a lambda filter referencing session state.


# ── History pagination ──────────────────────────────────────────

@router.callback_query(F.data.startswith("history:"))
async def on_history_page(callback: CallbackQuery):
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return
    offset = safe_int(callback.data.split(":")[1]) or 0
    await callback.answer()
    from app.bot.commands.user import _show_history_page
    await _show_history_page(callback, offset=max(0, offset))


# ── Redistribute ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("redistribute:"))
@require_admin_callback
async def on_redistribute(callback: CallbackQuery):
    action = callback.data.split(":")[1]
    if action == "cancel":
        await callback.answer("Ок")
        await callback.message.edit_text("⏭ Отменено.", parse_mode="HTML")
        return

    num = safe_int(action)
    if num is None:
        await callback.answer("Ошибка")
        return
    await callback.answer("Запускаю...")
    await callback.message.edit_text("⏳ Раскидываю треки...", parse_mode="HTML")
    result = await cmd_distribute_force(get_pool(), num, triggered_by=callback.from_user.id)
    await callback.message.edit_text(result["message"], parse_mode="HTML")


# ── Recap carousel ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("recap_page:"))
async def on_recap_page(callback: CallbackQuery):
    """Carousel navigation for recap blocks."""
    parts = callback.data.split(":")
    turdom_num = safe_int(parts[1])
    page = safe_int(parts[2])
    if turdom_num is None or page is None:
        await callback.answer("Ошибка")
        return

    async with get_pool().acquire() as conn:
        pl = await conn.fetchrow("SELECT spotify_id FROM playlists WHERE number = $1", turdom_num)
        recap_text = None
        if pl:
            recap_text = await conn.fetchval(
                """SELECT recap_text FROM sessions
                   WHERE playlist_id = (SELECT id FROM playlists WHERE spotify_id = $1) ORDER BY id DESC LIMIT 1""",
                pl["spotify_id"],
            )

    if not recap_text:
        await callback.answer("Рекап не найден")
        return

    blocks = [b.strip() for b in recap_text.split("\n\n---\n\n") if b.strip()]
    if page < 0 or page >= len(blocks):
        await callback.answer("Нет такой страницы")
        return

    kb = session.recap_keyboard(turdom_num, page, len(blocks), admin=is_admin(callback.from_user.id))
    await callback.message.edit_text(blocks[page], parse_mode="HTML", reply_markup=kb, link_preview_options=_NO_PREVIEW)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("rerecap:"))
@require_admin_callback
async def on_rerecap(callback: CallbackQuery):
    num = safe_int(callback.data.split(":")[1])
    if num is None:
        await callback.answer("Ошибка")
        return
    await callback.answer("Генерирую...")
    await callback.message.edit_reply_markup(reply_markup=None)

    result = await cmd_recap_regenerate(get_pool(), num, triggered_by=callback.from_user.id)

    if result["status"] == "ok" and result.get("recap_text"):
        await session.send_recap_carousel(callback.message.chat.id, result["recap_text"], num)
    else:
        await callback.message.answer(result["message"], parse_mode="HTML")


# ── Fuzzy duplicate confirmation ───────────────────────────────

@router.callback_query(F.data.startswith("confirm_dup:"))
async def on_confirm_dup(callback: CallbackQuery):
    """User confirmed fuzzy duplicate — remove from playlist."""
    if not await is_registered(callback.from_user.id):
        await callback.answer("⛔ Только для участников", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    playlist_spotify_id = parts[1]
    track_id = parts[2]

    # Guard against double-click
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        from app.spotify.auth import get_spotify
        sp = await get_spotify()
        await sp.playlist_remove(playlist_spotify_id, [f"spotify:track:{track_id}"])

        # Remove from DB — scoped to this playlist only
        async with get_pool().acquire() as conn:
            playlist_db_id = await conn.fetchval(
                "SELECT id FROM playlists WHERE spotify_id = $1", playlist_spotify_id
            )
            if playlist_db_id:
                await conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = $1 AND spotify_track_id = $2",
                    playlist_db_id, track_id,
                )

        await callback.answer("🗑 Удалено!")
        await callback.message.edit_text(
            f"{callback.message.text}\n\n🗑 <b>Удалено</b>",
            parse_mode="HTML",
        )
        log.info(f"Fuzzy duplicate confirmed and removed: {track_id} from {playlist_spotify_id}")
    except Exception as e:
        log.error(f"Failed to remove fuzzy duplicate: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith("keep_dup:"))
async def on_keep_dup(callback: CallbackQuery):
    """User said it's not a duplicate — keep it."""
    await callback.answer("✅ Оставлено")
    await callback.message.edit_text(
        f"{callback.message.text}\n\n✅ <b>Оставлено</b>",
        parse_mode="HTML",
    )
