"""Resolve genre for a track via Last.fm tags, AI classification, or Spotify artist genres.

Priority chain:
1. Last.fm track.getTopTags — most accurate (track-level, user-contributed)
2. AI classification — GPT classifies by track name + artist into TURDOM genres
3. Spotify artist genres — fallback (artist-level, often inaccurate for multi-genre artists)
"""
import asyncio
import logging

import asyncpg

from app.config import settings
from app.spotify.auth import get_spotify
from app.services.lastfm import get_track_tags
from app.services.genre_distributor import classify_track, GENRE_MAP

log = logging.getLogger(__name__)

# TURDOM genre categories for AI classification
_GENRE_NAMES = [name.replace("TURDOM ", "") for name in GENRE_MAP.keys()]


async def resolve_genre(track) -> str:
    """Resolve genre string for a Spotify track object.

    Tries: Last.fm → AI → Spotify artist genres.
    Returns comma-separated genre string, or 'unknown'.
    """
    title = track.name
    artist = ", ".join(a.name for a in track.artists) if track.artists else ""

    # 1. Last.fm
    genre = await _resolve_lastfm(title, artist)
    if genre:
        log.debug(f"Genre via Last.fm for '{title}': {genre}")
        return genre

    # 2. AI classification
    genre = await _resolve_ai(title, artist)
    if genre:
        log.debug(f"Genre via AI for '{title}': {genre}")
        return genre

    # 3. Spotify artist genres (fallback)
    genre = await _resolve_spotify(track)
    if genre:
        log.debug(f"Genre via Spotify for '{title}': {genre}")
        return genre

    return "unknown"


async def _resolve_lastfm(title: str, artist: str) -> str | None:
    """Try Last.fm track tags. Returns genre string if classifiable."""
    tags = await get_track_tags(title, artist)
    if not tags:
        return None

    # Try to classify each tag through GENRE_MAP
    for tag in tags:
        classified = classify_track(tag)
        if classified:
            return tag  # Return raw tag — classify_track() maps it in distribute

    # If no single tag matches, try comma-joined
    joined = ", ".join(tags[:5])
    classified = classify_track(joined)
    if classified:
        return joined

    return None


async def _resolve_ai(title: str, artist: str) -> str | None:
    """Ask AI to classify track into a TURDOM genre category."""
    if not settings.openai_api_key:
        return None

    try:
        from app.services.ai import get_openai

        client = get_openai()

        genres_list = ", ".join(_GENRE_NAMES)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Определи жанр трека '{title}' исполнителя '{artist}'. "
                    f"Выбери ОДИН из: {genres_list}. "
                    f"Ответь только названием жанра, одним словом или фразой. "
                    f"Если не знаешь — ответь 'unknown'."
                ),
            }],
            max_tokens=20,
            temperature=0,
        )

        result = resp.choices[0].message.content.strip().lower()
        if result and result != "unknown":
            # Verify it maps to a TURDOM playlist
            for genre_name in _GENRE_NAMES:
                if genre_name.lower() == result:
                    return result
            # Try classify_track as fallback
            if classify_track(result):
                return result

        return None
    except Exception as e:
        log.debug(f"AI genre classification failed for '{title}': {e}")
        return None


async def _resolve_spotify(track) -> str | None:
    """Fallback: Spotify artist genres."""
    if not track.artists:
        return None

    sp = await get_spotify()

    for artist_ref in track.artists:
        try:
            artist = await sp.artist(artist_ref.id)
            genres = artist.genres or []
            if genres:
                return ", ".join(genres)
        except Exception as e:
            log.warning(f"Failed to fetch artist {artist_ref.id} for genre: {e}")

    return None


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

    async with pool.acquire() as conn:
        for row in tracks:
            track_id = row["spotify_track_id"]
            try:
                track = await sp.track(track_id)
                genre = await resolve_genre(track)

                await conn.execute(
                    "UPDATE playlist_tracks SET genre = $1 WHERE spotify_track_id = $2",
                    genre, track_id,
                )

                processed += 1
                if genre != "unknown":
                    resolved += 1

                # Rate limit: avoid hammering Last.fm/OpenAI/Spotify
                await asyncio.sleep(0.25)
            except Exception as e:
                log.warning(f"Failed to backfill genre for {track_id}: {e}")
                processed += 1

    log.info(f"Genre backfill done: {processed} processed, {resolved} resolved")
    return {"processed": processed, "resolved": resolved}
