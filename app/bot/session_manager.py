"""SessionManager — encapsulates all session state and lifecycle methods."""

import asyncio
import logging

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.spotify.auth import get_spotify
from app.spotify.monitor import SpotifyMonitor, TrackInfo
from app.services.voting import record_vote, remove_track_from_playlist, skip_to_next, create_session_track
from app.services.ai import generate_track_facts, generate_pre_recap_teaser
from app.services.admin_commands import RecapProgress
from app.services.genre_distributor import distribute_session_tracks
from app.services.track_formatter import build_track_caption
from app.utils import display_name

log = logging.getLogger(__name__)


class SessionManager:
    """Manages the lifecycle of a listening session."""

    def __init__(self):
        self.monitor = SpotifyMonitor()
        self.reset()

    def reset(self):
        """Clear all session state."""
        self.active_session_id: int | None = None
        self.active_playlist_id: str | None = None
        self.current_session_track_id: int | None = None
        self.participants: set[int] = set()
        self.track_messages: dict[int, list[tuple[int, int, str]]] = {}
        self.played_track_ids: set[str] = set()
        self.cached_pre_recap: str | None = None
        self.skip_in_progress: set[int] = set()
        self.session_message: tuple[int, int] | None = None
        self.waiting_secret_clarification: dict[int, dict] = {}
        self.session_end_prompted: bool = False
        self.waiting_theme: bool = False

    async def get_participant_names(self) -> str:
        """Get formatted list of participant names."""
        from app.bot.core import get_pool as _get_pool
        names = []
        async with _get_pool().acquire() as conn:
            for tid in self.participants:
                row = await conn.fetchrow(
                    "SELECT telegram_username, telegram_name FROM users WHERE telegram_id = $1", tid
                )
                if row:
                    name = display_name(row["telegram_username"], row["telegram_name"])
                    names.append(name)
                else:
                    names.append(str(tid))
        return ", ".join(names) if names else "—"

    async def update_session_message(self):
        """Update the session creation message with current participant list."""
        from app.bot.core import get_pool as _get_pool, edit_text
        if not self.session_message or not self.active_session_id:
            return
        try:
            chat_id, msg_id = self.session_message
            start_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
            ]])
            async with _get_pool().acquire() as conn:
                playlist_name = await conn.fetchval(
                    "SELECT p.name FROM sessions s JOIN playlists p ON s.playlist_id = p.id WHERE s.id = $1",
                    self.active_session_id,
                )
            names = await self.get_participant_names()
            await edit_text(
                chat_id, msg_id,
                f"🎧 Сессия создана: <b>{playlist_name}</b>\n"
                f"👥 Участников: {len(self.participants)} — {names}\n\n"
                f"Жди пока все присоединятся, потом жми кнопку.",
                reply_markup=start_kb,
            )
        except Exception as e:
            log.debug(f"Failed to edit session message: {e}")

    async def on_track_change(self, info: TrackInfo):
        """Handle Spotify track change event."""
        from app.bot.core import bot, get_pool as _get_pool, is_admin, send, send_photo

        if self.active_session_id is None:
            return

        if info.track_id in self.played_track_ids:
            log.info(f"Track {info.track_id} already played — skipping")
            try:
                sp = await get_spotify()
                await sp.playback_next()
            except Exception as e:
                log.error(f"Failed to skip already played track: {e}")
            return

        self.played_track_ids.add(info.track_id)

        # Finalize previous track: auto-keep for those who didn't vote, show result
        # Skip if already finalized by voting flow (skip_in_progress)
        if self.current_session_track_id is not None and self.current_session_track_id not in self.skip_in_progress:
            async with _get_pool().acquire() as conn:
                prev_result = await conn.fetchrow(
                    "SELECT vote_result FROM session_tracks WHERE id = $1",
                    self.current_session_track_id,
                )
                if prev_result and prev_result["vote_result"] == "pending":
                    await conn.execute(
                        "UPDATE session_tracks SET vote_result = 'keep' WHERE id = $1",
                        self.current_session_track_id,
                    )
                # Get vote counts for result text
                keep_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'keep'",
                    self.current_session_track_id,
                )
                drop_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'drop'",
                    self.current_session_track_id,
                )
                final_result = await conn.fetchval(
                    "SELECT vote_result FROM session_tracks WHERE id = $1",
                    self.current_session_track_id,
                )

            if final_result and final_result != "pending":
                emoji = "✅" if final_result == "keep" else "❌"
                result_text = f"{keep_count} за / {drop_count} против — {emoji} {final_result}"
                await self.finalize_track_card(self.current_session_track_id, result_text)
            elif self.current_session_track_id in self.track_messages:
                # Just remove buttons if no votes at all
                for chat_id, message_id, _ in self.track_messages[self.current_session_track_id]:
                    try:
                        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                    except Exception as e:
                        log.debug(f"Failed to edit message: {e}")

        session_track_id = await create_session_track(_get_pool(), self.active_session_id, info)
        self.current_session_track_id = session_track_id

        # Persist current track to DB for recovery
        async with _get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET current_track_id = $1 WHERE id = $2",
                session_track_id, self.active_session_id,
            )

        # Resolve added_by
        added_by_name = None
        if info.added_by:
            async with _get_pool().acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT telegram_username, telegram_name FROM users WHERE spotify_id = $1", info.added_by
                )
                if row:
                    added_by_name = display_name(row["telegram_username"], row["telegram_name"])

        if added_by_name:
            added_by_text = f"\n👤 {added_by_name}"
        elif info.added_by:
            added_by_text = f"\n👤 <code>{info.added_by}</code>"
        else:
            added_by_text = ""

        # Check cached AI facts first, then generate
        cached_facts = None
        async with _get_pool().acquire() as conn:
            cached_facts = await conn.fetchval(
                "SELECT ai_facts FROM tracks WHERE spotify_track_id = $1 AND ai_facts IS NOT NULL",
                info.track_id,
            )

        if cached_facts:
            facts = cached_facts
        else:
            facts = await generate_track_facts(info.title, info.artist, info.album, release_date=info.release_date)
            if facts:
                async with _get_pool().acquire() as conn:
                    await conn.execute(
                        "UPDATE tracks SET ai_facts = $1 WHERE spotify_track_id = $2",
                        facts, info.track_id,
                    )

        VOTE_RESULT_RESERVE = 50
        text = build_track_caption(
            info.title, info.artist, info.album, info.track_id,
            facts=facts or "", added_by_text=added_by_text,
            max_caption=1024 - VOTE_RESULT_RESERVE,
        )

        vote_row = [
            InlineKeyboardButton(text="✅ Keep", callback_data=f"vote:keep:{session_track_id}"),
            InlineKeyboardButton(text="❌ Drop", callback_data=f"vote:drop:{session_track_id}"),
        ]
        spotify_url = f"https://open.spotify.com/track/{info.track_id}"
        link_row = [
            InlineKeyboardButton(text="🎧 Spotify", url=spotify_url),
        ]

        sent_messages = []
        for tid in self.participants:
            try:
                fire_row = [
                    InlineKeyboardButton(text="🔥", callback_data=f"fire:{session_track_id}"),
                ]
                if is_admin(tid):
                    rows = [vote_row, [
                        InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{session_track_id}"),
                        InlineKeyboardButton(text="🔄 Факты", callback_data=f"regen_facts:{session_track_id}"),
                    ], fire_row + link_row]
                else:
                    rows = [vote_row, fire_row + link_row]
                kb = InlineKeyboardMarkup(inline_keyboard=rows)

                if info.cover_url:
                    msg = await send_photo(tid, info.cover_url, text, reply_markup=kb)
                else:
                    msg = await send(tid, text, reply_markup=kb)
                sent_messages.append((tid, msg.message_id, text))
            except Exception as e:
                log.warning(f"Failed to send track to {tid}: {e}")

        self.track_messages[session_track_id] = sent_messages

        # Persist to DB for recovery
        async with _get_pool().acquire() as conn:
            for chat_id, msg_id, caption in sent_messages:
                await conn.execute(
                    """INSERT INTO track_messages (session_track_id, chat_id, message_id, caption)
                       VALUES ($1, $2, $3, $4) ON CONFLICT (session_track_id, chat_id) DO NOTHING""",
                    session_track_id, chat_id, msg_id, caption,
                )

    async def update_vote_buttons(self, session_track_id: int):
        """Update voting button labels with current counts."""
        from app.bot.core import bot, get_pool as _get_pool, is_admin

        if session_track_id not in self.track_messages:
            return

        async with _get_pool().acquire() as conn:
            keep_count = await conn.fetchval(
                "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'keep'", session_track_id
            )
            drop_count = await conn.fetchval(
                "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'drop'", session_track_id
            )
            stid = await conn.fetchval(
                "SELECT t.spotify_track_id FROM session_tracks st JOIN tracks t ON st.track_id = t.id WHERE st.id = $1",
                session_track_id,
            )

        keep_text = f"✅ Keep ({keep_count})" if keep_count > 0 else "✅ Keep"
        drop_text = f"❌ Drop ({drop_count})" if drop_count > 0 else "❌ Drop"

        extra_row = [InlineKeyboardButton(text="🔥", callback_data=f"fire:{session_track_id}")]
        if stid:
            extra_row.append(InlineKeyboardButton(text="🎧 Spotify", url=f"https://open.spotify.com/track/{stid}"))

        for chat_id, message_id, _ in self.track_messages[session_track_id]:
            try:
                vote_row = [
                    InlineKeyboardButton(text=keep_text, callback_data=f"vote:keep:{session_track_id}"),
                    InlineKeyboardButton(text=drop_text, callback_data=f"vote:drop:{session_track_id}"),
                ]
                if is_admin(chat_id):
                    rows = [vote_row, [
                        InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{session_track_id}"),
                        InlineKeyboardButton(text="🔄 Факты", callback_data=f"regen_facts:{session_track_id}"),
                    ], extra_row]
                else:
                    rows = [vote_row, extra_row]
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
            except Exception as e:
                log.debug(f"Failed to edit message: {e}")

    async def finalize_track_card(self, session_track_id: int, result_text: str):
        """Update track card with result and remove voting buttons."""
        from app.bot.core import bot, edit_text, edit_caption

        if session_track_id not in self.track_messages:
            return

        for chat_id, message_id, caption in self.track_messages[session_track_id]:
            try:
                new_caption = f"{caption}\n\n{result_text}"
                if len(new_caption) > 1024:
                    new_caption = new_caption[:1021] + "…"
                await edit_caption(chat_id, message_id, new_caption)
            except Exception as e:
                log.debug(f"Failed to edit caption: {e}")
                try:
                    new_text = f"{caption}\n\n{result_text}"
                    await edit_text(chat_id, message_id, new_text)
                except Exception as e2:
                    log.debug(f"Failed to edit text: {e2}")
                    try:
                        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                    except Exception as e3:
                        log.debug(f"Failed to edit message: {e3}")

    async def check_session_complete(self):
        """Check if all tracks have been voted on — suggest ending."""
        from app.bot.core import get_pool as _get_pool, send

        if self.active_session_id is None:
            return

        if self.session_end_prompted:
            return

        async with _get_pool().acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM session_tracks WHERE session_id = $1 AND vote_result = 'pending'",
                self.active_session_id,
            )
            voted = await conn.fetchval(
                "SELECT COUNT(*) FROM session_tracks WHERE session_id = $1 AND vote_result != 'pending'",
                self.active_session_id,
            )
            # Count tracks in the actual playlist
            playlist_total = await conn.fetchval(
                """SELECT COUNT(*) FROM playlist_tracks pt
                   JOIN sessions s ON s.playlist_id = pt.playlist_id
                   WHERE s.id = $1""",
                self.active_session_id,
            )

        # Don't prompt if we haven't heard most of the playlist yet
        if pending == 0 and playlist_total > 0 and voted >= playlist_total * 0.8:
            try:
                sp = await get_spotify()
                await sp.playback_pause()
                log.info("Paused playback — all tracks voted")
            except Exception as e:
                log.warning(f"Failed to pause playback: {e}")

            self.session_end_prompted = True
            end_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏁 Завершить сессию", callback_data="confirm_end"),
                InlineKeyboardButton(text="▶️ Продолжить", callback_data="continue_session"),
            ]])
            await send(
                settings.telegram_admin_id,
                "⏸ <b>Все треки оценены!</b> Плейлист на паузе.\n\nЗавершить сессию?",
                reply_markup=end_kb,
            )

    async def end_session(self):
        """End the active session: stop monitor, finalize votes, send recap."""
        from app.bot.core import bot, get_pool as _get_pool, send
        from app.services.admin_commands import _generate_and_save_recap, log_action

        if self.active_session_id is None:
            return

        session_id_to_end = self.active_session_id
        await self.monitor.stop()

        async with _get_pool().acquire() as conn:
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

        # Pre-recap teaser + progress messages
        teaser = self.cached_pre_recap or "🎧 Ну что, чем всё закончилось? Сейчас узнаем! 🥁"
        progress_msgs: dict[int, int] = {}
        recap_progress = RecapProgress()
        initial_text = recap_progress.render()
        for tid in self.participants:
            try:
                await send(tid, teaser)
                msg = await bot.send_message(tid, initial_text)
                progress_msgs[tid] = msg.message_id
            except Exception as e:
                log.debug(f"Failed to notify {tid}: {e}")

        # Background task: poll progress and edit messages every 2 sec
        async def _update_progress():
            last_text = ""
            while True:
                await asyncio.sleep(2)
                text = recap_progress.render()
                if text != last_text:
                    for tid, mid in progress_msgs.items():
                        try:
                            await bot.edit_message_text(text, chat_id=tid, message_id=mid)
                        except Exception:
                            pass
                    last_text = text
                # Keep typing indicator alive
                for tid in progress_msgs:
                    try:
                        await bot.send_chat_action(tid, "typing")
                    except Exception:
                        pass

        progress_task = asyncio.create_task(_update_progress())

        # Generate full recap
        turdom_number = None
        async with _get_pool().acquire() as conn:
            turdom_number = await conn.fetchval(
                "SELECT p.number FROM playlists p JOIN sessions s ON s.playlist_id = p.id WHERE s.id = $1",
                session_id_to_end,
            )

        recap_text = await _generate_and_save_recap(
            _get_pool(), session_id_to_end, turdom_number or 0, None,
            progress=recap_progress,
        )

        # Stop progress updater
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        # Remove progress messages before sending recap
        for tid, mid in progress_msgs.items():
            try:
                await bot.delete_message(tid, mid)
            except Exception as e:
                log.debug(f"Failed to delete progress msg for {tid}: {e}")

        if recap_text:
            for tid in self.participants:
                try:
                    await self.send_recap_carousel(tid, recap_text, turdom_number or 0)
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")
        else:
            for tid in self.participants:
                try:
                    await send(tid, "❌ Не удалось сгенерировать рекап — попробуй /recap позже")
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")

        # Distribute kept tracks to genre playlists
        try:
            dist_result = await distribute_session_tracks(_get_pool(), session_id_to_end)
            if dist_result["distributed"] > 0:
                dist_msg = f"🎶 Раскидал {dist_result['distributed']} треков по жанровым плейлистам!"
                for tid in self.participants:
                    try:
                        await send(tid, dist_msg)
                    except Exception as e:
                        log.debug(f"Failed to notify {tid}: {e}")
            async with _get_pool().acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET distributed_at = NOW() WHERE id = $1",
                    session_id_to_end,
                )
        except Exception as e:
            log.error(f"Genre distribution failed: {e}")

        # Log action
        try:
            await log_action(
                _get_pool(), "end_session",
                session_id=session_id_to_end,
                result={"total": stats["total"], "kept": stats["kept"], "dropped": stats["dropped"]},
            )
        except Exception as e:
            log.warning(f"Failed to log end_session action: {e}")

        # Offer to create next playlist
        post_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Обычный", callback_data="create_playlist:normal"),
             InlineKeyboardButton(text="🎭 Тематический", callback_data="create_playlist:thematic")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="create_playlist:skip")],
        ])
        await send(
            settings.telegram_admin_id,
            "🆕 Создать следующий плейлист?",
            reply_markup=post_kb,
        )

        # Clean up track_messages for ended session
        try:
            async with _get_pool().acquire() as conn:
                await conn.execute(
                    """DELETE FROM track_messages WHERE session_track_id IN
                       (SELECT id FROM session_tracks WHERE session_id = $1)""",
                    session_id_to_end,
                )
        except Exception as e:
            log.debug(f"Failed to clean track_messages: {e}")

        self.reset()

    @staticmethod
    def recap_keyboard(turdom_num: int, page: int, total: int, admin: bool = False) -> InlineKeyboardMarkup:
        """Build carousel keyboard for recap blocks."""
        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="←", callback_data=f"recap_page:{turdom_num}:{page - 1}"))
        buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data="noop"))
        if page < total - 1:
            buttons.append(InlineKeyboardButton(text="→", callback_data=f"recap_page:{turdom_num}:{page + 1}"))
        rows = [buttons]
        if admin:
            rows.append([InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"rerecap:{turdom_num}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def send_recap_carousel(self, chat_id: int, recap_text: str, turdom_num: int, user_id: int | None = None):
        """Send first recap block as carousel with navigation."""
        from app.bot.core import send, is_admin
        blocks = [b.strip() for b in recap_text.split("\n\n---\n\n") if b.strip()]
        if not blocks:
            return
        kb = self.recap_keyboard(turdom_num, 0, len(blocks), admin=is_admin(user_id or chat_id))
        await send(chat_id, blocks[0], reply_markup=kb)

    async def cache_pre_recap_teaser(self):
        """Pre-generate recap teaser in background."""
        from app.bot.core import get_pool as _get_pool
        try:
            sp = await get_spotify()
            all_items = []
            offset = 0
            while True:
                pl_items = await sp.playlist_items(self.active_playlist_id, limit=100, offset=offset)
                all_items.extend(pl_items.items)
                offset += len(pl_items.items)
                if offset >= pl_items.total:
                    break
            total = len(all_items)

            contributors = {}
            for item in all_items:
                if item.added_by:
                    uid = item.added_by.id
                    contributors[uid] = contributors.get(uid, 0) + 1

            top_spotify_id = max(contributors, key=contributors.get) if contributors else None
            top_name = None
            if top_spotify_id:
                async with _get_pool().acquire() as conn:
                    row = await conn.fetchrow("SELECT telegram_name FROM users WHERE spotify_id = $1", top_spotify_id)
                    if row:
                        top_name = row["telegram_name"]

            participant_names = []
            async with _get_pool().acquire() as conn:
                for tid in self.participants:
                    row = await conn.fetchrow("SELECT telegram_name FROM users WHERE telegram_id = $1", tid)
                    if row:
                        participant_names.append(row["telegram_name"])

            self.cached_pre_recap = await generate_pre_recap_teaser(total, participant_names, top_name)
            log.info(f"Pre-recap teaser cached: {self.cached_pre_recap[:50]}...")
        except Exception as e:
            log.warning(f"Failed to generate pre-recap teaser: {e}")
            self.cached_pre_recap = "🎧 Ну что, чем всё закончилось? Сейчас узнаем! 🥁"

    async def recover(self):
        """Recover active session state from DB after bot restart."""
        from app.bot.core import get_pool as _get_pool
        async with _get_pool().acquire() as conn:
            active = await conn.fetchrow(
                """SELECT s.id, p.spotify_id as playlist_spotify_id, s.current_track_id
                   FROM sessions s JOIN playlists p ON s.playlist_id = p.id
                   WHERE s.status = 'active' ORDER BY s.id DESC LIMIT 1"""
            )
            if not active:
                return

            self.active_session_id = active["id"]
            self.active_playlist_id = active["playlist_spotify_id"]
            self.current_session_track_id = active["current_track_id"]

            rows = await conn.fetch(
                "SELECT telegram_id FROM session_participants WHERE session_id = $1 AND active = TRUE",
                self.active_session_id,
            )
            self.participants = {r["telegram_id"] for r in rows}

            played = await conn.fetch(
                "SELECT spotify_track_id FROM session_tracks WHERE session_id = $1",
                self.active_session_id,
            )
            self.played_track_ids = {r["spotify_track_id"] for r in played}

            tm_rows = await conn.fetch(
                """SELECT tm.session_track_id, tm.chat_id, tm.message_id, tm.caption
                   FROM track_messages tm
                   JOIN session_tracks st ON tm.session_track_id = st.id
                   WHERE st.session_id = $1""",
                self.active_session_id,
            )
            for r in tm_rows:
                stid = r["session_track_id"]
                if stid not in self.track_messages:
                    self.track_messages[stid] = []
                self.track_messages[stid].append((r["chat_id"], r["message_id"], r["caption"]))

            log.info(
                f"Recovered active session {self.active_session_id}: "
                f"{len(self.participants)} participants, {len(self.played_track_ids)} played tracks, "
                f"{len(self.track_messages)} track messages"
            )

            # Restart monitor
            self.monitor.on_track_change(lambda info: asyncio.ensure_future(self.on_track_change(info)))
            asyncio.create_task(self.monitor.start(self.active_playlist_id))


# Singleton instance
session = SessionManager()
