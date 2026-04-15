"""Resolve genre for a track via Last.fm tags or AI classification.

Priority chain:
1. Last.fm track.getTopTags — most accurate (track-level, user-contributed)
2. AI classification — GPT classifies by track name + artist into TURDOM genres
3. No fallback — returns None (admin handles manually)

Spotify artist genres removed — too inaccurate for track-level classification.
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


async def resolve_genre(track) -> str | None:
    """Resolve genre string for a Spotify track object.

    Tries: Last.fm → AI. Returns comma-separated genre tags, or None.
    Stores up to 5 classifiable tags for multi-genre distribution.
    """
    title = track.name
    artist = ", ".join(a.name for a in track.artists) if track.artists else ""

    # 1. Last.fm — returns multiple tags
    genre = await _resolve_lastfm(title, artist)
    if genre:
        log.debug(f"Genre via Last.fm for '{title}': {genre}")
        return genre

    # 2. AI classification
    genre = await _resolve_ai(title, artist)
    if genre:
        log.debug(f"Genre via AI for '{title}': {genre}")
        return genre

    # 3. No fallback — return None for manual handling
    log.debug(f"No genre found for '{title}' by '{artist}'")
    return None


async def _resolve_lastfm(title: str, artist: str) -> str | None:
    """Try Last.fm track tags. Returns up to 5 classifiable tags as comma-separated string."""
    tags = await get_track_tags(title, artist)
    if not tags:
        return None

    # Collect all tags that map to a TURDOM playlist (up to 5)
    classifiable = []
    for tag in tags:
        if classify_track(tag) and tag not in classifiable:
            classifiable.append(tag)
            if len(classifiable) >= 5:
                break

    if classifiable:
        return ", ".join(classifiable)

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
                    f"Выбери ОДИН или НЕСКОЛЬКО из: {genres_list}. "
                    f"Ответь только названиями жанров через запятую. "
                    f"Если не знаешь — ответь 'unknown'."
                ),
            }],
            max_tokens=50,
            temperature=0,
        )

        result = resp.choices[0].message.content.strip().lower()
        if result and result != "unknown":
            # Verify each part maps to a TURDOM playlist
            parts = [p.strip() for p in result.split(",")]
            valid = [p for p in parts if classify_track(p)]
            if valid:
                return ", ".join(valid)

        return None
    except Exception as e:
        log.debug(f"AI genre classification failed for '{title}': {e}")
        return None


async def resolve_and_save_genre(pool: asyncpg.Pool, track) -> str | None:
    """Resolve genre and save it to all playlist_tracks rows for this track."""
    genre = await resolve_genre(track)

    if genre:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tracks SET genre = $1 WHERE spotify_track_id = $2 AND (genre IS NULL)",
                genre, track.id,
            )
        log.debug(f"Genre for '{track.name}': {genre}")

    return genre


async def backfill_genres(pool: asyncpg.Pool) -> dict:
    """Backfill genres for all tracks. Resets and re-resolves via Last.fm + AI."""
    async with pool.acquire() as conn:
        tracks = await conn.fetch(
            "SELECT DISTINCT spotify_track_id FROM tracks WHERE genre IS NULL"
        )

    if not tracks:
        return {"processed": 0, "resolved": 0, "unknown": 0}

    sp = await get_spotify()
    processed = 0
    resolved = 0

    async with pool.acquire() as conn:
        for row in tracks:
            track_id = row["spotify_track_id"]
            try:
                track = await sp.track(track_id)
                genre = await resolve_genre(track)

                if genre:
                    await conn.execute(
                        "UPDATE tracks SET genre = $1 WHERE spotify_track_id = $2",
                        genre, track_id,
                    )
                    resolved += 1

                processed += 1
                await asyncio.sleep(0.25)
            except Exception as e:
                log.warning(f"Failed to backfill genre for {track_id}: {e}")
                processed += 1

    unknown = processed - resolved
    log.info(f"Genre backfill done: {processed} processed, {resolved} resolved, {unknown} unknown")
    return {"processed": processed, "resolved": resolved, "unknown": unknown}
