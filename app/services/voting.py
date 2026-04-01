import logging

import asyncpg
import tekore as tk

from app.config import settings
from app.spotify.auth import get_spotify

log = logging.getLogger(__name__)


async def record_vote(pool: asyncpg.Pool, session_track_id: int, telegram_id: int, vote: str, session_id: int | None = None) -> dict:
    """Record a vote and check if threshold is reached. Returns vote status."""
    async with pool.acquire() as conn:
        # Check if already voted
        existing = await conn.fetchrow(
            "SELECT vote FROM votes WHERE session_track_id = $1 AND telegram_id = $2",
            session_track_id, telegram_id,
        )
        vote_changed = False
        if existing:
            if existing["vote"] == vote:
                return {"status": "already_voted"}
            # Change vote
            await conn.execute(
                "UPDATE votes SET vote = $1, voted_at = NOW() WHERE session_track_id = $2 AND telegram_id = $3",
                vote, session_track_id, telegram_id,
            )
            vote_changed = True
        else:
            await conn.execute(
                "INSERT INTO votes (session_track_id, telegram_id, vote) VALUES ($1, $2, $3)",
                session_track_id, telegram_id, vote,
            )

        # Count drop votes
        drop_count = await conn.fetchval(
            "SELECT COUNT(*) FROM votes WHERE session_track_id = $1 AND vote = 'drop'",
            session_track_id,
        )
        total_votes = await conn.fetchval(
            "SELECT COUNT(*) FROM votes WHERE session_track_id = $1",
            session_track_id,
        )

        # Drop if >= 50% of session participants voted drop
        if session_id:
            participant_count = await conn.fetchval(
                "SELECT COUNT(*) FROM session_participants WHERE session_id = $1 AND active = TRUE",
                session_id,
            )
        else:
            participant_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users"
            )
        drop_threshold = max(1, (participant_count + 1) // 2)  # ceil(50%) — e.g. 4 people = 2, 3 people = 2, 5 people = 3

        # Determine and persist vote result
        vote_result = None
        if drop_count >= drop_threshold:
            await conn.execute(
                "UPDATE session_tracks SET vote_result = 'drop' WHERE id = $1",
                session_track_id,
            )
            vote_result = "drop"
        elif total_votes >= participant_count and drop_count < drop_threshold:
            await conn.execute(
                "UPDATE session_tracks SET vote_result = 'keep' WHERE id = $1",
                session_track_id,
            )
            vote_result = "keep"

        status = "vote_changed" if vote_changed else "recorded"
        return {
            "status": status,
            "vote_result": vote_result,
            "drop_count": drop_count,
            "total_votes": total_votes,
            "threshold": drop_threshold,
            "participants": participant_count,
        }


async def remove_track_from_playlist(playlist_id: str, track_id: str):
    """Remove a track from Spotify playlist."""
    try:
        sp = await get_spotify()
        await sp.playlist_remove(playlist_id, [f"spotify:track:{track_id}"])
        log.info(f"Removed track {track_id} from playlist {playlist_id}")
    except Exception as e:
        log.error(f"Failed to remove track: {e}")


async def skip_to_next():
    """Skip to next track in Spotify."""
    try:
        sp = await get_spotify()
        await sp.playback_next()
        log.info("Skipped to next track")
    except Exception as e:
        log.error(f"Failed to skip: {e}")


async def create_session_track(pool: asyncpg.Pool, session_id: int, track_info) -> int:
    """Insert a new track into session_tracks, return its id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO session_tracks (session_id, spotify_track_id, title, artist, album, cover_url, added_by_spotify_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            session_id, track_info.track_id, track_info.title, track_info.artist,
            track_info.album, track_info.cover_url, track_info.added_by,
        )
        return row["id"]
