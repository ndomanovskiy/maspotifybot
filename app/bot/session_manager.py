"""SessionManager — encapsulates all session state and lifecycle methods."""

import asyncio
import logging

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.spotify.auth import get_spotify
from app.spotify.monitor import SpotifyMonitor, TrackInfo
from app.services.voting import record_vote, remove_track_from_playlist, skip_to_next, create_session_track
from app.services.ai import generate_track_facts, generate_pre_recap_teaser
from app.services.genre_distributor import distribute_session_tracks
from app.services.track_formatter import format_track, format_album

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
        from app.bot.core import pool
        names = []
        async with pool.acquire() as conn:
            for tid in self.participants:
                row = await conn.fetchrow(
                    "SELECT telegram_username, telegram_name FROM users WHERE telegram_id = $1", tid
                )
                if row:
                    name = f"@{row['telegram_username']}" if row["telegram_username"] else row["telegram_name"]
                    names.append(name)
                else:
                    names.append(str(tid))
        return ", ".join(names) if names else "—"

    async def update_session_message(self):
        """Update the session creation message with current participant list."""
        from app.bot.core import pool, edit_text
        if not self.session_message or not self.active_session_id:
            return
        try:
            chat_id, msg_id = self.session_message
            start_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Запустить прослушивание!", callback_data="start_listening")
            ]])
            async with pool.acquire() as conn:
                playlist_name = await conn.fetchval(
                    "SELECT playlist_name FROM sessions WHERE id = $1", self.active_session_id
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
        from app.bot.core import bot, pool, is_admin, send, send_photo

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

        # Remove voting buttons from previous track card
        if self.current_session_track_id is not None and self.current_session_track_id in self.track_messages:
            for chat_id, message_id, _ in self.track_messages[self.current_session_track_id]:
                try:
                    await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                except Exception as e:
                    log.debug(f"Failed to edit message: {e}")

        session_track_id = await create_session_track(pool, self.active_session_id, info)
        self.current_session_track_id = session_track_id

        # Persist current track to DB for recovery
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET current_track_id = $1 WHERE id = $2",
                session_track_id, self.active_session_id,
            )

        # Resolve added_by
        added_by_name = None
        if info.added_by:
            async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
            cached_facts = await conn.fetchval(
                "SELECT ai_facts FROM playlist_tracks WHERE spotify_track_id = $1 AND ai_facts IS NOT NULL LIMIT 1",
                info.track_id,
            )

        if cached_facts:
            facts = cached_facts
        else:
            facts = await generate_track_facts(info.title, info.artist, info.album)
            if facts:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE playlist_tracks SET ai_facts = $1 WHERE spotify_track_id = $2",
                        facts, info.track_id,
                    )

        facts_text = f"\n\n💡 {facts}" if facts else ""

        track_display = format_track(info.title, info.artist, info.track_id)

        VOTE_RESULT_RESERVE = 50
        MAX_CAPTION = 1024 - VOTE_RESULT_RESERVE

        album_display = format_album(info.album)

        text = (
            f"🎵 {track_display}\n"
            f"💿 {album_display}"
            f"{added_by_text}{facts_text}"
        )

        # Trim facts if too long
        if len(text) > MAX_CAPTION and facts_text:
            header = (
                f"🎵 {track_display}\n"
                f"💿 {album_display}"
                f"{added_by_text}"
            )
            available = MAX_CAPTION - len(header) - 3
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
        for tid in self.participants:
            try:
                if is_admin(tid):
                    rows = [vote_row, [
                        InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{session_track_id}"),
                        InlineKeyboardButton(text="🔄 Факты", callback_data=f"regen_facts:{session_track_id}"),
                    ]]
                else:
                    rows = [vote_row]
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
        async with pool.acquire() as conn:
            for chat_id, msg_id, caption in sent_messages:
                await conn.execute(
                    """INSERT INTO track_messages (session_track_id, chat_id, message_id, caption)
                       VALUES ($1, $2, $3, $4) ON CONFLICT (session_track_id, chat_id) DO NOTHING""",
                    session_track_id, chat_id, msg_id, caption,
                )

    async def update_vote_buttons(self, session_track_id: int):
        """Update voting button labels with current counts."""
        from app.bot.core import bot, pool, is_admin

        if session_track_id not in self.track_messages:
            return

        async with pool.acquire() as conn:
            keep_count = await conn.fetchval(
                "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'keep'", session_track_id
            )
            drop_count = await conn.fetchval(
                "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'drop'", session_track_id
            )

        keep_text = f"✅ Keep ({keep_count})" if keep_count > 0 else "✅ Keep"
        drop_text = f"❌ Drop ({drop_count})" if drop_count > 0 else "❌ Drop"

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
                    ]]
                else:
                    rows = [vote_row]
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
        from app.bot.core import pool, send

        if self.active_session_id is None:
            return

        if self.session_end_prompted:
            return

        async with pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM session_tracks WHERE session_id = $1 AND vote_result = 'pending'",
                self.active_session_id,
            )

        if pending == 0:
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
        from app.bot.core import bot, pool, send
        from app.services.admin_commands import _generate_and_save_recap, log_action

        if self.active_session_id is None:
            return

        session_id_to_end = self.active_session_id
        await self.monitor.stop()

        async with pool.acquire() as conn:
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

        # Pre-recap teaser
        teaser = self.cached_pre_recap or "🎧 Ну что, чем всё закончилось? Сейчас узнаем! 🥁"
        for tid in self.participants:
            try:
                await send(tid, teaser)
                await bot.send_chat_action(tid, "typing")
            except Exception as e:
                log.debug(f"Failed to notify {tid}: {e}")

        # Generate full recap
        turdom_number = None
        async with pool.acquire() as conn:
            turdom_number = await conn.fetchval(
                "SELECT p.number FROM playlists p JOIN sessions s ON s.playlist_spotify_id = p.spotify_id WHERE s.id = $1",
                session_id_to_end,
            )

        recap_text = await _generate_and_save_recap(pool, session_id_to_end, turdom_number or 0, None)

        if recap_text:
            for tid in self.participants:
                try:
                    await self.send_recap_carousel(tid, recap_text, turdom_number or 0)
                except Exception as e:
                    log.debug(f"Failed to notify {tid}: {e}")

        # Distribute kept tracks to genre playlists
        try:
            dist_result = await distribute_session_tracks(pool, session_id_to_end)
            if dist_result["distributed"] > 0:
                dist_msg = f"🎶 Раскидал {dist_result['distributed']} треков по жанровым плейлистам!"
                for tid in self.participants:
                    try:
                        await send(tid, dist_msg)
                    except Exception as e:
                        log.debug(f"Failed to notify {tid}: {e}")
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET distributed_at = NOW() WHERE id = $1",
                    session_id_to_end,
                )
        except Exception as e:
            log.error(f"Genre distribution failed: {e}")

        # Log action
        try:
            await log_action(
                pool, "end_session",
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

        self.reset()

    @staticmethod
    def recap_keyboard(turdom_num: int, page: int, total: int) -> InlineKeyboardMarkup:
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

    async def send_recap_carousel(self, chat_id: int, recap_text: str, turdom_num: int):
        """Send first recap block as carousel with navigation."""
        from app.bot.core import send
        blocks = [b.strip() for b in recap_text.split("\n\n---\n\n") if b.strip()]
        if not blocks:
            return
        kb = self.recap_keyboard(turdom_num, 0, len(blocks))
        await send(chat_id, blocks[0], reply_markup=kb)

    async def cache_pre_recap_teaser(self):
        """Pre-generate recap teaser in background."""
        from app.bot.core import pool
        try:
            sp = await get_spotify()
            pl_items = await sp.playlist_items(self.active_playlist_id, limit=100)
            total = pl_items.total

            contributors = {}
            for item in pl_items.items:
                if item.added_by:
                    uid = item.added_by.id
                    contributors[uid] = contributors.get(uid, 0) + 1

            top_spotify_id = max(contributors, key=contributors.get) if contributors else None
            top_name = None
            if top_spotify_id:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT telegram_name FROM users WHERE spotify_id = $1", top_spotify_id)
                    if row:
                        top_name = row["telegram_name"]

            participant_names = []
            async with pool.acquire() as conn:
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
        from app.bot.core import pool
        async with pool.acquire() as conn:
            active = await conn.fetchrow(
                "SELECT id, playlist_spotify_id, current_track_id FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
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
