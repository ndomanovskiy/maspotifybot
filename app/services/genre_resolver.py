"""Resolve genre for a track via Spotify artist genres.

Spotify API does not provide genres on tracks — only on artists.
This service fetches the primary artist's genres and returns them
as a comma-separated string suitable for storing in playlist_tracks.genre.
"""
import logging

import asyncpg

from app.spotify.auth import get_spotify

log = logging.getLogger(__name__)


async def resolve_genre(track) -> str:
    """Resolve genre string for a Spotify track object.

    Tries each artist's genres until one has them.
    Returns comma-separated genre string, or 'unknown' if no genres found.
    """
    if not track.artists:
        return "unknown"

    sp = await get_spotify()

    for artist_ref in track.artists:
        try:
            artist = await sp.artist(artist_ref.id)
            genres = artist.genres or []
            if genres:
                return ", ".join(genres)
        except Exception as e:
            log.warning(f"Failed to fetch artist {artist_ref.id} for genre: {e}")

    return "unknown"


async def resolve_and_save_genre(pool: asyncpg.Pool, track) -> str:
    """Resolve genre and save it to all playlist_tracks rows for this track."""
    genre = await resolve_genre(track)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE playlist_tracks SET genre = $1 WHERE spotify_track_id = $2 AND (genre IS NULL OR genre = 'unknown')",
            genre, track.id,
        )

    if genre != "unknown":
        log.debug(f"Genre for '{track.name}': {genre}")

    return genre


async def backfill_genres(pool: asyncpg.Pool) -> dict:
    """Backfill genres for all tracks missing them. Returns stats."""
    async with pool.acquire() as conn:
        tracks = await conn.fetch(
            "SELECT DISTINCT spotify_track_id FROM playlist_tracks WHERE genre IS NULL OR genre = 'unknown'"
        )

    if not tracks:
        return {"processed": 0, "resolved": 0}

    sp = await get_spotify()
    processed = 0
    resolved = 0

    for row in tracks:
        track_id = row["spotify_track_id"]
        try:
            track = await sp.track(track_id)
            genre = await resolve_genre(track)

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE playlist_tracks SET genre = $1 WHERE spotify_track_id = $2",
                    genre, track_id,
                )

            processed += 1
            if genre != "unknown":
                resolved += 1
        except Exception as e:
            log.warning(f"Failed to backfill genre for {track_id}: {e}")
            processed += 1

    log.info(f"Genre backfill done: {processed} processed, {resolved} resolved")
    return {"processed": processed, "resolved": resolved}
