"""Resolve genre for a track via Last.fm tags or AI classification.

Priority chain:
1. Last.fm track.getTopTags — most accurate (track-level, user-contributed)
2. Last.fm artist.getTopTags — the track's artists in turn; less exact for one track, but real data
3. AI classification — Claude classifies by track name + artist into TURDOM genres
4. No fallback — returns None (the watcher asks whoever added the track)

A source that did not answer is not the same as one that knows nothing: when nothing
was found and a source was silent, resolve_genre raises GenreLookupFailed instead.
A source that cannot be used at all (no key, a refused key, no credit) counts as absent.

Spotify artist genres removed — too inaccurate for track-level classification.
"""
import asyncio
import logging

import asyncpg

from app.spotify.auth import get_spotify
from app.services.ai import ask_claude
from app.services.lastfm import get_artist_tags, get_track_tags
from app.services.genre_distributor import classify_track, GENRE_MAP, PICK_TAGS

log = logging.getLogger(__name__)

# TURDOM genre categories for AI classification
_GENRE_NAMES = [name.removeprefix("TURDOM ") for name in GENRE_MAP]

# Claude answers with those names; "hip-hop" or "dnb" would match no playlist, so each becomes its tag
_AI_TAGS = {name.removeprefix("TURDOM ").lower(): tag for name, tag in PICK_TAGS.items()}

# A compilation can list a dozen artists, and each one costs a Last.fm request
_MAX_ARTISTS = 3


class GenreLookupFailed(Exception):
    """Nothing knew the genre, and a source that might have known it did not answer."""


async def resolve_genre(track) -> str | None:
    """Resolve genre string for a Spotify track object.

    Tries: Last.fm track → Last.fm artists → AI. Returns comma-separated genre tags,
    or None when every source answered and none knew the genre.
    Raises GenreLookupFailed when none knew it but Last.fm or AI did not answer.
    Stores up to 5 classifiable tags for multi-genre distribution.
    """
    title = track.name
    artist = ", ".join(a.name for a in track.artists) if track.artists else ""
    silent: list[str] = []  # sources that did not answer

    # 1. Last.fm — returns multiple tags
    track_tags = await get_track_tags(title, artist)
    genre = _classifiable(track_tags or [])
    if genre:
        log.debug(f"Genre via Last.fm for '{title}': {genre}")
        return genre

    # 2. Last.fm tags of the track's artists, unless Last.fm is not answering at all
    if track_tags is None:
        silent.append("Last.fm")
    else:
        genre, answered = await _resolve_lastfm_artists([a.name for a in track.artists or []])
        if genre:
            log.debug(f"Genre via Last.fm artist tags for '{title}': {genre}")
            return genre
        if not answered:
            silent.append("Last.fm")

    # 3. AI classification
    try:
        genre = await _resolve_ai(title, artist)
    except Exception as e:
        log.warning(f"AI genre classification failed for '{title}': {e}")
        silent.append("AI")
    else:
        if genre:
            log.debug(f"Genre via AI for '{title}': {genre}")
            return genre

    if silent:
        raise GenreLookupFailed(f"no genre for '{title}': {' and '.join(silent)} did not answer")

    # 4. No fallback — return None for manual handling
    log.debug(f"No genre found for '{title}' by '{artist}'")
    return None


async def _resolve_lastfm_artists(artists: list[str]) -> tuple[str | None, bool]:
    """Try Last.fm tags of each artist in turn; the first one with classifiable tags decides.

    The flag is False when Last.fm stopped answering before the artists ran out.
    """
    for name in artists[:_MAX_ARTISTS]:
        tags = await get_artist_tags(name)
        if tags is None:
            return None, False
        genre = _classifiable(tags)
        if genre:
            return genre, True
    return None, True


def _classifiable(tags: list[str]) -> str | None:
    """Up to 5 of the tags that map to a TURDOM playlist, comma-separated, or None."""
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
    """Ask Claude to classify track into a TURDOM genre category.

    None when Claude does not know or cannot be used at all; raises when it did not answer.
    """
    genres_list = ", ".join(_GENRE_NAMES)
    answer = await ask_claude(
        f"Определи жанр трека '{title}' исполнителя '{artist}'. "
        f"Выбери ОДИН или НЕСКОЛЬКО из: {genres_list}. "
        f"Ответь только названиями жанров через запятую. "
        f"Если не знаешь — ответь 'unknown'.",
        max_tokens=50,
    )

    result = (answer or "").strip().lower()
    if result and result != "unknown":
        # Verify each part maps to a TURDOM playlist
        parts = [_AI_TAGS.get(p.strip(), p.strip()) for p in result.split(",")]
        valid = [p for p in parts if classify_track(p)]
        if valid:
            return ", ".join(dict.fromkeys(valid))

    return None


async def resolve_and_save_genre(pool: asyncpg.Pool, track) -> str | None:
    """Make sure the track has a genre: return the saved one, or resolve and save it.

    A genre the track already has, resolved earlier or picked by a person, is kept
    and not looked up again. None means every source answered and nobody knows the genre;
    GenreLookupFailed from resolve_genre passes through when a source did not answer.
    """
    async with pool.acquire() as conn:
        saved = await conn.fetchval("SELECT genre FROM tracks WHERE spotify_track_id = $1", track.id)
    if saved:
        return saved

    genre = await resolve_genre(track)

    if genre:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tracks SET genre = $1 WHERE spotify_track_id = $2 AND (genre IS NULL)",
                genre, track.id,
            )
        log.debug(f"Genre for '{track.name}': {genre}")

    return genre


async def save_genre_if_missing(pool: asyncpg.Pool, track_db_id: int, genre: str) -> bool:
    """Save a genre a person picked, unless the track got one meanwhile. True if it was saved.

    genre_approved marks it as a person's pick, which /backfill_genres reset keeps.
    """
    async with pool.acquire() as conn:
        saved_id = await conn.fetchval(
            "UPDATE tracks SET genre = $1, genre_approved = TRUE WHERE id = $2 AND genre IS NULL RETURNING id",
            genre, track_db_id,
        )
    return saved_id is not None


async def backfill_genres(pool: asyncpg.Pool) -> dict:
    """Resolve genres for tracks that have none, via Last.fm track and artist tags + Claude.

    A genre saved while the run goes on (a person's pick) is never overwritten.
    """
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
                    status = await conn.execute(
                        "UPDATE tracks SET genre = $1 WHERE spotify_track_id = $2 AND genre IS NULL",
                        genre, track_id,
                    )
                    if status != "UPDATE 0":  # 0: a person picked a genre while the run went on
                        resolved += 1
            except Exception as e:
                log.warning(f"Failed to backfill genre for {track_id}: {e}")
            processed += 1
            await asyncio.sleep(0.25)  # after a failure too: a rate-limited Last.fm needs the pause most

    unknown = processed - resolved
    log.info(f"Genre backfill done: {processed} processed, {resolved} resolved, {unknown} unknown")
    return {"processed": processed, "resolved": resolved, "unknown": unknown}
