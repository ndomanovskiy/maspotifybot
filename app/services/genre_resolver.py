"""Resolve genre for a track: Last.fm and Deezer gather what they know, Claude decides with it.

Sources:
1. Last.fm track.getTopTags — most accurate (track-level, user-contributed)
2. Last.fm artist.getTopTags — the track's artists in turn, when the track has no tags; less exact for one track
3. Deezer by ISRC — the genre the music distributor gave the release; broad, but known for little-known tracks
Claude then picks one to three TURDOM genres, with what the sources found as hints, so it can make a broad
«Electro» exact (DnB, Phonk). Without Claude's answer the first source that knew decides, in the order above.
Nobody knowing returns None (the watcher asks whoever added the track).

A source that did not answer is not the same as one that knows nothing: when nothing
was found and a source was silent, resolve_genre raises GenreLookupFailed instead.
A source that cannot be used at all (no key, a refused key, no credit) counts as absent.

Spotify artist genres removed — too inaccurate for track-level classification.
"""
import asyncio
import logging
import re

import asyncpg

from app.spotify.auth import get_spotify
from app.services.ai import ask_claude
from app.services.deezer import get_release_genre_tags
from app.services.lastfm import get_artist_tags, get_track_tags
from app.services.genre_distributor import classify_track, classify_track_multi, GENRE_MAP, PICK_TAGS

log = logging.getLogger(__name__)

# TURDOM genre categories for AI classification
_GENRE_NAMES = [name.removeprefix("TURDOM ") for name in GENRE_MAP]

# Claude answers with those names; "hip-hop" or "dnb" would match no playlist, so each becomes its tag.
# The tags themselves ("hip hop", "drum and bass") are accepted too.
_AI_TAGS = {name.removeprefix("TURDOM ").lower(): tag for name, tag in PICK_TAGS.items()} | {
    tag: tag for tag in PICK_TAGS.values()
}

# A compilation can list a dozen artists, and each one costs a Last.fm request
_MAX_ARTISTS = 3

# How many genres Claude may give a track
_MAX_AI_GENRES = 3


class GenreLookupFailed(Exception):
    """Nothing knew the genre, and a source that might have known it did not answer."""


async def resolve_genre(track, isrc: str | None = None) -> str | None:
    """Resolve genre string for a Spotify track object.

    Last.fm (track, else artists) and Deezer gather what they know, then Claude decides with it.
    Returns comma-separated genre tags, or None when every source answered and none knew the genre.
    Raises GenreLookupFailed when none knew it but Last.fm, Deezer or Claude did not answer.
    `isrc` is for Deezer; without it the one Spotify gave the track is used.
    """
    title = track.name
    artist = ", ".join(a.name for a in track.artists) if track.artists else ""
    silent: list[str] = []  # sources that did not answer
    found: dict[str, str] = {}  # what each source knew, in the order it decides without Claude

    # 1. Last.fm — returns multiple tags
    track_tags = await get_track_tags(title, artist)
    if track_tags is None:
        silent.append("Last.fm")
    elif genre := _classifiable(track_tags):
        found["Last.fm, теги трека"] = genre
    else:
        # 2. Last.fm tags of the track's artists
        genre, answered = await _resolve_lastfm_artists([a.name for a in track.artists or []])
        if genre:
            found["Last.fm, теги исполнителя"] = genre
        if not answered:
            silent.append("Last.fm")

    # 3. Deezer: the genre the music distributor gave the release
    if isrc is None:
        from app.services.playlists import extract_isrc  # playlists imports this module

        isrc = extract_isrc(track)
    release_tags = await get_release_genre_tags(isrc)
    if release_tags is None:
        silent.append("Deezer")
    elif genre := _classifiable(release_tags):
        found["Deezer, жанр релиза"] = genre

    # Claude decides, with what the sources found
    try:
        genre = await _resolve_ai(title, artist, found)
    except Exception as e:
        log.warning(f"AI genre classification failed for '{title}': {e}")
        silent.append("AI")
    else:
        if genre:
            log.debug(f"Genre via Claude for '{title}': {genre} (hints: {found})")
            return genre

    # Without Claude's answer the first source that knew decides
    if found:
        source, genre = next(iter(found.items()))
        log.debug(f"Genre via {source} for '{title}': {genre}")
        return genre

    if silent:
        raise GenreLookupFailed(f"no genre for '{title}': {' and '.join(silent)} did not answer")

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


async def _resolve_ai(title: str, artist: str, hints: dict[str, str]) -> str | None:
    """Ask Claude to pick the track's TURDOM genres, with what other sources found as hints.

    None when Claude does not know or cannot be used at all; raises when it did not answer.
    """
    parts = [f"Определи жанр трека «{title}» исполнителя «{artist}»."]
    if hints:
        known = "; ".join(f"{source}: {_hint(genre)}" for source, genre in hints.items())
        parts.append(f"Что о нём известно: {known}. Уточни, если знаешь точнее.")
    parts.append(f"Выбери от одного до трёх из: {', '.join(_GENRE_NAMES)}.")
    parts.append("Ответь только названиями жанров через запятую. Если не знаешь — ответь 'unknown'.")
    answer = await ask_claude(" ".join(parts), max_tokens=50)

    text = (answer or "").lower()
    if re.search(r"\bunknown\b|не знаю", text):  # unsure, even with a genre next to it
        return None
    # Notes in brackets go first, a cut-off one too: a comma inside must not split them into genres
    text = re.sub(r"\([^)]*\)?", "", text)
    # Only the genre names (or their tags) count: anything else in the answer is dropped
    named = [_AI_TAGS.get(_genre_name(part)) for part in re.split(r"[,;/\n]", text)]
    picked = list(dict.fromkeys(tag for tag in named if tag))[:_MAX_AI_GENRES]
    return ", ".join(picked) if picked else None


def _genre_name(part: str) -> str:
    """One genre from Claude's answer without a list marker or quotes around it."""
    part = re.sub(r"^\s*(?:[-*•>#]+|\d+[.)])\s*", "", part)
    return part.strip().strip(".*_'\"«»`“”‘’„").strip()


def _hint(genre: str) -> str:
    """A source's tags with the playlists they lead to, so Claude sees the bot's own mapping."""
    playlists = ", ".join(name.removeprefix("TURDOM ") for name in classify_track_multi(genre))
    return f"{playlists} ({genre})"


async def resolve_and_save_genre(pool: asyncpg.Pool, track, isrc: str | None = None) -> str | None:
    """Make sure the track has a genre: return the saved one, or resolve and save it.

    A genre the track already has, resolved earlier or picked by a person, is kept
    and not looked up again. None means every source answered and nobody knows the genre;
    GenreLookupFailed from resolve_genre passes through when a source did not answer.
    `isrc` is the one the caller already found, for Deezer.
    """
    async with pool.acquire() as conn:
        saved = await conn.fetchval("SELECT genre FROM tracks WHERE spotify_track_id = $1", track.id)
    if saved:
        return saved

    genre = await resolve_genre(track, isrc)

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
    """Resolve genres for tracks that have none, via Last.fm track and artist tags, Deezer + Claude.

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
