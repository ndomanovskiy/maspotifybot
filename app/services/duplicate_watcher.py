import asyncio
import logging
from datetime import datetime

import asyncpg

from app.spotify.auth import get_spotify
from app.services.playlists import check_duplicate, get_track_isrc

log = logging.getLogger(__name__)


class DuplicateWatcher:
    def __init__(self, pool: asyncpg.Pool, notify_callback):
        self._pool = pool
        self._notify = notify_callback  # async fn(telegram_id, track_title, artist, duplicates)
        self._running = False
        self._known_tracks: dict[str, set[str]] = {}  # playlist_spotify_id -> set of track_ids

    async def start(self):
        self._running = True
        log.info("Duplicate watcher started")

        # Load known tracks from DB
        await self._load_known_tracks()

        while self._running:
            try:
                interval = self._get_interval()
                await self._check_playlists()
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

    async def _load_known_tracks(self):
        """Load all known tracks from DB to avoid false positives."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.spotify_id as playlist_spotify_id, pt.spotify_track_id
                FROM playlist_tracks pt JOIN playlists p ON pt.playlist_id = p.id
                WHERE p.status IN ('active', 'upcoming')
                """
            )
        for row in rows:
            pid = row["playlist_spotify_id"]
            if pid not in self._known_tracks:
                self._known_tracks[pid] = set()
            self._known_tracks[pid].add(row["spotify_track_id"])

        log.info(f"Loaded known tracks for {len(self._known_tracks)} playlists")

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

            if playlist_spotify_id not in self._known_tracks:
                self._known_tracks[playlist_spotify_id] = set()

            known = self._known_tracks[playlist_spotify_id]

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

                if track.id in known:
                    continue  # Already known

                # New track found!
                known.add(track.id)

                # Save to DB
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)

                added_by = item.added_by.id if item.added_by else None

                async with self._pool.acquire() as conn:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO playlist_tracks (playlist_id, spotify_track_id, isrc, title, artist, added_by_spotify_id, added_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (playlist_id, spotify_track_id) DO NOTHING
                            """,
                            playlist_db_id, track.id, isrc,
                            track.name, ", ".join(a.name for a in track.artists),
                            added_by, item.added_at if hasattr(item, "added_at") else None,
                        )
                    except Exception as e:
                        log.warning(f"Failed to save new track: {e}")

                # Check for duplicates in OTHER playlists
                if not isrc:
                    isrc = await get_track_isrc(track.id)

                duplicates = await check_duplicate(self._pool, track.id, isrc)
                # Filter out current playlist from results
                duplicates = [d for d in duplicates if d["playlist"] != pl["name"]]

                if duplicates:
                    # Find who added it (Telegram ID)
                    telegram_id = None
                    if added_by:
                        async with self._pool.acquire() as conn:
                            row = await conn.fetchrow(
                                "SELECT telegram_id FROM users WHERE spotify_id = $1", added_by
                            )
                            if row:
                                telegram_id = row["telegram_id"]

                    await self._notify(
                        telegram_id=telegram_id,
                        track_title=track.name,
                        artist=", ".join(a.name for a in track.artists),
                        duplicates=duplicates,
                        playlist_name=pl["name"],
                    )

            log.debug(f"Checked playlist {pl['name']}: {len(known)} tracks tracked")
