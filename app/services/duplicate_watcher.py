import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from app.spotify.auth import get_spotify
from app.services.playlists import check_duplicate, classify_duplicates, check_siblings, get_track_isrc
from app.services.ai import generate_track_facts
from app.services.genre_resolver import resolve_and_save_genre
from app.services.genre_distributor import check_previously_dropped
from app.services.normalize import normalize_title, normalize_artist, base_title
from app.utils import display_name

log = logging.getLogger(__name__)


class DuplicateWatcher:
    def __init__(self, pool: asyncpg.Pool, notify_callback, confirm_callback=None,
                 drop_warn_callback=None, sibling_warn_callback=None):
        self._pool = pool
        self._notify = notify_callback
        self._confirm = confirm_callback
        self._drop_warn = drop_warn_callback
        self._sibling_warn = sibling_warn_callback
        self._running = False

    async def start(self):
        self._running = True
        log.info("Duplicate watcher started")

        # Wait a bit for Spotify auth to load
        await asyncio.sleep(5)

        while self._running:
            try:
                interval = self._get_interval()
                await self._check_playlists()
                await self._generate_missing_facts()
                await asyncio.sleep(interval)
            except Exception as e:
                log.error(f"Duplicate watcher error: {e}")
                await asyncio.sleep(60)

    async def stop(self):
        self._running = False
        log.info("Duplicate watcher stopped")

    def _get_interval(self) -> int:
        """Get polling interval in seconds based on day of week and time."""
        now = datetime.now(timezone.utc)
        # Convert to Moscow time (UTC+3)
        moscow_hour = (now.hour + 3) % 24
        weekday = now.weekday()  # 0=Mon, 6=Sun

        if weekday == 2:  # Wednesday
            if moscow_hour < 14:
                return 3600  # 1 hour
            else:
                return 86400  # session handles it
        elif weekday == 0:  # Monday
            return 17280  # ~5x per day (86400/5)
        elif weekday == 1:  # Tuesday
            return 3600  # 1 hour
        elif weekday in (3, 4, 5, 6):  # Thu-Sun
            return 43200  # 2x per day (86400/2)

        return 43200  # default

    async def _resolve_telegram_id(self, spotify_id: str | None) -> int | None:
        """Resolve Spotify user ID to Telegram ID."""
        if not spotify_id:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_id FROM users WHERE spotify_id = $1", spotify_id
            )
            return row["telegram_id"] if row else None

    async def _resolve_user(self, spotify_id: str | None) -> tuple[int | None, str | None]:
        """Resolve Spotify user ID to (telegram_id, display_name)."""
        if not spotify_id:
            return None, None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_id, telegram_username, telegram_name FROM users WHERE spotify_id = $1",
                spotify_id,
            )
            if row:
                return row["telegram_id"], display_name(row["telegram_username"], row["telegram_name"])
        return None, None

    async def _handle_duplicate(self, sp, pl, track, track_artist, real_duplicates,
                                added_by, playlist_spotify_id, playlist_db_id):
        """Handle a confirmed duplicate: auto-remove or ask for confirmation."""
        telegram_id, added_by_name = await self._resolve_user(added_by)

        # Split by match type and session status
        has_exact_kept = any(
            d["match"] in ("exact", "isrc") and d.get("session_status") in ("keep", "pending")
            for d in real_duplicates
        )
        dropped_only = [d for d in real_duplicates if d.get("session_status") == "drop"]
        fuzzy_only = [d for d in real_duplicates if d["match"].startswith("fuzzy_")]

        if has_exact_kept:
            # Hard duplicate: was kept in a session → auto-remove
            try:
                await sp.playlist_remove(playlist_spotify_id, [f"spotify:track:{track.id}"])
                log.info(f"Auto-removed duplicate {track.name} from {pl['name']}")
            except Exception as e:
                log.error(f"Failed to auto-remove duplicate: {e}")

            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = $1 AND spotify_track_id = $2",
                    playlist_db_id, track.id,
                )
            await self._notify(
                telegram_id=telegram_id,
                track_title=track.name,
                artist=track_artist,
                duplicates=real_duplicates,
                playlist_name=pl["name"],
                track_id=track.id,
                added_by_name=added_by_name,
            )
        elif dropped_only and self._confirm:
            await self._confirm(
                telegram_id=telegram_id,
                track_title=track.name,
                artist=track_artist,
                duplicates=dropped_only,
                playlist_name=pl["name"],
                track_id=track.id,
                playlist_spotify_id=playlist_spotify_id,
                added_by_name=added_by_name,
            )
        elif fuzzy_only and self._confirm:
            await self._confirm(
                telegram_id=telegram_id,
                track_title=track.name,
                artist=track_artist,
                duplicates=fuzzy_only,
                playlist_name=pl["name"],
                track_id=track.id,
                playlist_spotify_id=playlist_spotify_id,
                added_by_name=added_by_name,
            )

    async def _get_known_track_ids(self, playlist_db_id: int) -> set[str]:
        """Load all known track IDs for a playlist from DB in one query."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT t.spotify_track_id FROM playlist_tracks pt
                   JOIN tracks t ON pt.track_id = t.id
                   WHERE pt.playlist_id = $1""",
                playlist_db_id,
            )
        return {row["spotify_track_id"] for row in rows}

    async def _generate_missing_facts(self):
        """Generate AI facts for upcoming playlist tracks that don't have them yet."""
        async with self._pool.acquire() as conn:
            tracks = await conn.fetch(
                """
                SELECT t.id, t.spotify_track_id, t.title, t.artist, t.album
                FROM tracks t
                JOIN playlist_tracks pt ON pt.track_id = t.id
                JOIN playlists p ON pt.playlist_id = p.id
                WHERE p.status = 'upcoming' AND t.ai_facts IS NULL
                """
            )

        if not tracks:
            return

        log.info(f"Generating AI facts for {len(tracks)} tracks")
        sp = await get_spotify()
        async with self._pool.acquire() as conn:
            for t in tracks:
                try:
                    # Fetch release_date from Spotify
                    release_date = ""
                    try:
                        track_info = await sp.track(t["spotify_track_id"])
                        if track_info.album and hasattr(track_info.album, "release_date"):
                            release_date = track_info.album.release_date or ""
                    except Exception:
                        pass

                    facts = await generate_track_facts(
                        t["title"], t["artist"], t["album"] or "",
                        release_date=release_date,
                    )
                    if facts:
                        await conn.execute(
                            "UPDATE tracks SET ai_facts = $1 WHERE id = $2",
                            facts, t["id"],
                        )
                        log.info(f"Generated facts for '{t['title']}'")
                except Exception as e:
                    log.warning(f"Failed to generate facts for '{t['title']}': {e}")

    async def _check_playlists(self):
        """Check active/upcoming playlists for new tracks and detect duplicates.

        Flow for each track in Spotify playlist:
        1. Check duplicates FIRST (before saving to DB)
        2. If duplicate → handle (remove/warn), don't save
        3. If clean → save to DB, resolve genre, check drops/siblings
        """
        async with self._pool.acquire() as conn:
            playlists = await conn.fetch(
                "SELECT id, spotify_id, name, is_thematic FROM playlists WHERE status IN ('active', 'upcoming')"
            )

        sp = await get_spotify()

        for pl in playlists:
            playlist_spotify_id = pl["spotify_id"]
            playlist_db_id = pl["id"]
            is_thematic = pl["is_thematic"]

            # known_ids: only used to skip DB insert for already-saved tracks
            known_ids = await self._get_known_track_ids(playlist_db_id)

            # Fetch current tracks from Spotify (with pagination)
            try:
                all_items = []
                offset = 0
                while True:
                    items = await sp.playlist_items(playlist_spotify_id, limit=100, offset=offset)
                    all_items.extend(items.items)
                    offset += len(items.items)
                    if offset >= items.total:
                        break
            except Exception as e:
                log.warning(f"Failed to fetch playlist {pl['name']}: {e}")
                continue

            # Track IDs already checked for duplicates this cycle (avoid re-checking)
            checked_ids: set[str] = set()

            for item in all_items:
                if item.track is None or item.track.id is None:
                    continue
                track = item.track
                added_by = item.added_by.id if item.added_by else None
                track_artist = ", ".join(a.name for a in track.artists)

                # Get ISRC once per track
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)

                # ── Step 1: Check duplicates (skip thematic, skip already-checked) ──
                if not is_thematic and track.id not in checked_ids:
                    checked_ids.add(track.id)

                    if not isrc:
                        isrc = await get_track_isrc(track.id)

                    duplicates = await check_duplicate(
                        self._pool, track.id, isrc,
                        title=track.name, artist=track_artist,
                    )
                    # Filter self-matches from same playlist
                    duplicates = [
                        d for d in duplicates
                        if d.get("playlist_id") != pl["id"] or d["match"].startswith("fuzzy_")
                    ]

                    if duplicates:
                        # Classify by session history
                        duplicates = await classify_duplicates(self._pool, duplicates)
                        real_duplicates = [d for d in duplicates if d.get("session_status") != "phantom"]

                        if real_duplicates:
                            await self._handle_duplicate(
                                sp, pl, track, track_artist, real_duplicates,
                                added_by, playlist_spotify_id, playlist_db_id,
                            )
                            continue  # Don't save duplicate to DB

                # ── Step 2: Save to DB (only if not duplicate and not yet known) ──
                if track.id not in known_ids:

                    async with self._pool.acquire() as conn:
                        try:
                            track_db_id = await conn.fetchval(
                                """INSERT INTO tracks (spotify_track_id, title, artist, isrc,
                                                        normalized_title, normalized_artist, normalized_base)
                                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                                   ON CONFLICT (spotify_track_id) DO UPDATE
                                      SET title = EXCLUDED.title, artist = EXCLUDED.artist,
                                          normalized_title = EXCLUDED.normalized_title,
                                          normalized_artist = EXCLUDED.normalized_artist,
                                          normalized_base = EXCLUDED.normalized_base
                                   RETURNING id""",
                                track.id, track.name, track_artist, isrc,
                                normalize_title(track.name), normalize_artist(track_artist),
                                base_title(track.name),
                            )
                            await conn.execute(
                                """INSERT INTO playlist_tracks (playlist_id, track_id, spotify_track_id, added_by_spotify_id, added_at)
                                   VALUES ($1, $2, $3, $4, $5)
                                   ON CONFLICT (playlist_id, spotify_track_id) DO NOTHING""",
                                playlist_db_id, track_db_id, track.id, added_by,
                                item.added_at if hasattr(item, "added_at") else None,
                            )
                        except Exception as e:
                            log.warning(f"Failed to save new track: {e}")

                    # Resolve genre
                    try:
                        await resolve_and_save_genre(self._pool, track)
                    except Exception as e:
                        log.warning(f"Failed to resolve genre for '{track.name}': {e}")

                    # ── Step 3: Check drops/siblings (only for new non-duplicate tracks) ──
                    if not is_thematic:
                        drops = await check_previously_dropped(self._pool, track.id)
                        if drops and self._drop_warn:
                            telegram_id = await self._resolve_telegram_id(added_by)
                            await self._drop_warn(
                                telegram_id=telegram_id,
                                track_title=track.name,
                                artist=track_artist,
                                drops=drops,
                                playlist_name=pl["name"],
                                track_id=track.id,
                                playlist_spotify_id=playlist_spotify_id,
                            )

                        if self._sibling_warn and not drops:
                            siblings = await check_siblings(
                                self._pool, track.id, track.name, track_artist,
                                exclude_playlist_id=playlist_db_id,
                            )
                            if siblings:
                                telegram_id = await self._resolve_telegram_id(added_by)
                                await self._sibling_warn(
                                    telegram_id=telegram_id,
                                    track_title=track.name,
                                    artist=track_artist,
                                    siblings=siblings,
                                    playlist_name=pl["name"],
                                    track_id=track.id,
                                    playlist_spotify_id=playlist_spotify_id,
                                )

            # Cleanup: remove DB entries for tracks no longer in Spotify playlist
            spotify_ids = {item.track.id for item in all_items if item.track and item.track.id}
            stale_ids = known_ids - spotify_ids
            if stale_ids:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM playlist_tracks WHERE playlist_id = $1 AND spotify_track_id = ANY($2)",
                        playlist_db_id, list(stale_ids),
                    )
                log.info(f"Cleaned {len(stale_ids)} stale track(s) from '{pl['name']}'")


            log.debug(f"Checked playlist {pl['name']}")
