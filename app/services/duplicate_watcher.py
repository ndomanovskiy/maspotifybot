import asyncio
import logging
from datetime import datetime

import asyncpg

from app.spotify.auth import get_spotify
from app.services.playlists import check_duplicate, get_track_isrc
from app.services.ai import generate_track_facts
from app.services.genre_resolver import resolve_and_save_genre
from app.services.normalize import normalize_title, normalize_artist

log = logging.getLogger(__name__)


class DuplicateWatcher:
    def __init__(self, pool: asyncpg.Pool, notify_callback, confirm_callback=None):
        self._pool = pool
        self._notify = notify_callback  # async fn(telegram_id, track_title, artist, duplicates, playlist_name, track_id)
        self._confirm = confirm_callback  # async fn(telegram_id, track_title, artist, duplicates, playlist_name, track_id, playlist_spotify_id) — for fuzzy matches
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
        now = datetime.utcnow()
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

    async def _get_known_track_ids(self, playlist_db_id: int) -> set[str]:
        """Load all known track IDs for a playlist from DB in one query."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT spotify_track_id FROM playlist_tracks WHERE playlist_id = $1",
                playlist_db_id,
            )
        return {row["spotify_track_id"] for row in rows}

    async def _generate_missing_facts(self):
        """Generate AI facts for upcoming playlist tracks that don't have them yet."""
        async with self._pool.acquire() as conn:
            tracks = await conn.fetch(
                """
                SELECT pt.id, pt.spotify_track_id, pt.title, pt.artist
                FROM playlist_tracks pt
                JOIN playlists p ON pt.playlist_id = p.id
                WHERE p.status = 'upcoming' AND pt.ai_facts IS NULL
                """
            )

        if not tracks:
            return

        log.info(f"Generating AI facts for {len(tracks)} tracks")
        async with self._pool.acquire() as conn:
            for t in tracks:
                try:
                    facts = await generate_track_facts(t["title"], t["artist"], "")
                    if facts:
                        await conn.execute(
                            "UPDATE playlist_tracks SET ai_facts = $1 WHERE id = $2",
                            facts, t["id"],
                        )
                        log.info(f"Generated facts for '{t['title']}'")
                except Exception as e:
                    log.warning(f"Failed to generate facts for '{t['title']}': {e}")

    async def _check_playlists(self):
        """Check active/upcoming playlists for new tracks and detect duplicates."""
        async with self._pool.acquire() as conn:
            playlists = await conn.fetch(
                "SELECT id, spotify_id, name FROM playlists WHERE status IN ('active', 'upcoming')"
            )

        sp = await get_spotify()

        for pl in playlists:
            playlist_spotify_id = pl["spotify_id"]
            playlist_db_id = pl["id"]

            # Load all known track IDs for this playlist in one query
            known_ids = await self._get_known_track_ids(playlist_db_id)

            # Fetch current tracks from Spotify
            try:
                items = await sp.playlist_items(playlist_spotify_id, limit=100)
            except Exception as e:
                log.warning(f"Failed to fetch playlist {pl['name']}: {e}")
                continue

            for item in items.items:
                if item.track is None:
                    continue
                track = item.track

                if track.id in known_ids:
                    continue  # Already known

                # Save to DB
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)

                added_by = item.added_by.id if item.added_by else None

                track_artist = ", ".join(a.name for a in track.artists)
                track_album = track.album.name if track.album else ""

                async with self._pool.acquire() as conn:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO playlist_tracks (playlist_id, spotify_track_id, isrc, title, artist,
                                                         added_by_spotify_id, added_at, normalized_title, normalized_artist)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                            ON CONFLICT (playlist_id, spotify_track_id) DO NOTHING
                            """,
                            playlist_db_id, track.id, isrc,
                            track.name, track_artist,
                            added_by, item.added_at if hasattr(item, "added_at") else None,
                            normalize_title(track.name), normalize_artist(track_artist),
                        )
                    except Exception as e:
                        log.warning(f"Failed to save new track: {e}")

                # Resolve genre from Spotify artist
                try:
                    await resolve_and_save_genre(self._pool, track)
                except Exception as e:
                    log.warning(f"Failed to resolve genre for '{track.name}': {e}")

                # Check for duplicates in OTHER playlists
                if not isrc:
                    isrc = await get_track_isrc(track.id)

                duplicates = await check_duplicate(
                    self._pool, track.id, isrc,
                    title=track.name, artist=track_artist,
                )
                # Filter out current playlist from results
                duplicates = [d for d in duplicates if d["playlist"] != pl["name"]]

                if duplicates:
                    # Check if current playlist is thematic
                    async with self._pool.acquire() as conn:
                        is_thematic = await conn.fetchval(
                            "SELECT is_thematic FROM playlists WHERE id = $1", playlist_db_id
                        )

                    if is_thematic:
                        continue

                    # Find who added it (Telegram ID)
                    telegram_id = None
                    if added_by:
                        async with self._pool.acquire() as conn:
                            row = await conn.fetchrow(
                                "SELECT telegram_id FROM users WHERE spotify_id = $1", added_by
                            )
                            if row:
                                telegram_id = row["telegram_id"]

                    # Split: exact/isrc → auto-remove, fuzzy → ask user
                    has_exact = any(d["match"] in ("exact", "isrc") for d in duplicates)
                    fuzzy_only = [d for d in duplicates if d["match"].startswith("fuzzy_")]

                    if has_exact:
                        # Auto-remove
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
                            duplicates=duplicates,
                            playlist_name=pl["name"],
                            track_id=track.id,
                        )
                    elif fuzzy_only and self._confirm:
                        # Ask user to confirm
                        await self._confirm(
                            telegram_id=telegram_id,
                            track_title=track.name,
                            artist=track_artist,
                            duplicates=fuzzy_only,
                            playlist_name=pl["name"],
                            track_id=track.id,
                            playlist_spotify_id=playlist_spotify_id,
                        )

            log.debug(f"Checked playlist {pl['name']}")
