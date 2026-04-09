import asyncio
import logging
from dataclasses import dataclass

import tekore as tk

from app.spotify.auth import get_spotify

log = logging.getLogger(__name__)


@dataclass
class TrackInfo:
    track_id: str
    title: str
    artist: str
    album: str
    cover_url: str
    duration_ms: int
    progress_ms: int
    added_by: str | None = None


class SpotifyMonitor:
    def __init__(self):
        self._running = False
        self._current_track_id: str | None = None
        self._on_track_change = None
        self._on_pause = None
        self._on_resume = None
        self._on_end = None
        self._was_playing = False
        self._poll_interval = 4  # seconds
        self._not_playing_count = 0

    def on_track_change(self, callback):
        self._on_track_change = callback

    def on_pause(self, callback):
        self._on_pause = callback

    def on_resume(self, callback):
        self._on_resume = callback

    def on_end(self, callback):
        self._on_end = callback

    def on_suggest_skip(self, callback):
        self._on_suggest_skip = callback

    async def start(self, playlist_id: str):
        self._running = True
        self._current_track_id = None
        self._playlist_id = playlist_id
        self._not_playing_count = 0
        self._skip_suggested = False
        self._on_suggest_skip = getattr(self, "_on_suggest_skip", None)
        log.info(f"Monitor started for playlist {playlist_id}")

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                log.error(f"Monitor poll error: {e}")
            await asyncio.sleep(self._poll_interval)

    async def stop(self):
        self._running = False
        log.info("Monitor stopped")

    async def _poll(self):
        try:
            sp = await get_spotify()
        except Exception as e:
            log.warning(f"Monitor: failed to get Spotify client: {e}")
            return

        try:
            playback = await sp.playback_currently_playing()
        except Exception as e:
            log.warning(f"Monitor: Spotify API error (will retry): {e}")
            return

        if playback is None or playback.item is None:
            return

        track = playback.item
        is_playing = playback.is_playing

        # Check if 30% of track played — suggest skip if not yet suggested
        if (
            is_playing
            and track.id == self._current_track_id
            and not self._skip_suggested
            and playback.progress_ms
            and track.duration_ms
            and playback.progress_ms >= track.duration_ms * 0.30
        ):
            self._skip_suggested = True
            if self._on_suggest_skip:
                await self._on_suggest_skip()

        # Track change detection
        if track.id != self._current_track_id:
            old_id = self._current_track_id
            self._current_track_id = track.id
            self._skip_suggested = False

            if old_id is not None or True:  # always notify on first track too
                info = TrackInfo(
                    track_id=track.id,
                    title=track.name,
                    artist=", ".join(a.name for a in track.artists),
                    album=track.album.name,
                    cover_url=track.album.images[0].url if track.album.images else "",
                    duration_ms=track.duration_ms,
                    progress_ms=playback.progress_ms or 0,
                )

                # Try to get added_by from playlist context
                if hasattr(playback, "context") and playback.context:
                    info.added_by = await self._get_added_by(sp, track.id)

                if self._on_track_change:
                    await self._on_track_change(info)

        # Pause/resume detection
        if self._was_playing and not is_playing:
            if self._on_pause:
                await self._on_pause()
        elif not self._was_playing and is_playing:
            if self._on_resume:
                await self._on_resume()

        self._was_playing = is_playing

    async def _get_added_by(self, sp: tk.Spotify, track_id: str) -> str | None:
        """Get who added the track to the playlist."""
        try:
            items = await sp.playlist_items(self._playlist_id, limit=50)
            for item in items.items:
                if item.track and item.track.id == track_id:
                    return item.added_by.id if item.added_by else None
        except Exception as e:
            log.warning(f"Could not get added_by: {e}")
        return None
