"""Resolve genre for a track: Last.fm and Deezer gather what they know, Claude decides with it.

Sources:
1. Last.fm track.getTopTags — most accurate (track-level, user-contributed)
2. Last.fm artist.getTopTags — the track's artists in turn, when the track has no tags; less exact for one track
3. Deezer by ISRC — the genre the music distributor gave the release; broad, but known for little-known tracks
Claude then picks one to three TURDOM genres, with what the sources found as hints, so it can make a broad
«Electro» exact (DnB, Phonk). Without Claude's answer, sources that agree decide (see _disagree). Sources that
disagree raise GenreDisputed for the watcher to ask whoever added the track among the genres they named;
callers that ask nobody get the first source's genre, in the order above.
Nobody knowing returns None (the watcher asks whoever added the track among all genres).

A source that did not answer is not the same as one that knows nothing: when nothing
was found and a source was silent, resolve_genre raises GenreLookupFailed instead.
A source that cannot be used at all (no key, a refused key, no credit) counts as absent.

Spotify artist genres removed — too inaccurate for track-level classification.
"""
import asyncio
import functools
import itertools
import logging
import re
from collections.abc import Callable

import asyncpg

from app.spotify.auth import get_spotify
from app.services.ai import ask_claude
from app.services.deezer import get_release_genre_tags
from app.services.lastfm import get_artist_tags, get_track_tags
from app.services.genre_distributor import (
    classify_track, classify_track_multi, GENRE_MAP, PICK_TAGS, suppressing_genre,
)

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

# Saves a genre the bot found, for a track that has none yet
_SAVE_FOUND_GENRE = "UPDATE tracks SET genre = $1 WHERE spotify_track_id = $2 AND genre IS NULL RETURNING id"


class GenreLookupFailed(Exception):
    """Nothing knew the genre, and a source that might have known it did not answer."""


class GenreDisputed(Exception):
    """Claude gave no genre, and Last.fm and Deezer disagree: a person should pick.

    `sources` name the services that found a genre, `choices` are the tags of every playlist they
    named, in the order of the genre playlists. `fallback` is the first source's genre, for the track
    to keep until someone picks. `kept` is set by resolve_and_save_genre, not here: the fallback when
    it was saved, None when saving it failed.
    """

    def __init__(self, *, choices: list[str], fallback: str, sources: list[str]):
        super().__init__(f"{' and '.join(sources)} disagree: {', '.join(choices)}")
        self.choices = choices
        self.fallback = fallback
        self.sources = sources
        self.kept: str | None = None

    def __reduce__(self) -> tuple[functools.partial["GenreDisputed"], tuple[()], dict[str, object]]:
        """Copy and pickle rebuild an exception from its args, and this one takes keywords."""
        rebuild = functools.partial(type(self), choices=self.choices, fallback=self.fallback, sources=self.sources)
        return rebuild, (), self.__dict__


async def resolve_genre(track, isrc: str | None = None, *, ask_on_dispute: bool = False) -> str | None:
    """Resolve genre string for a Spotify track object.

    Last.fm (track, else artists) and Deezer gather what they know, then Claude decides with it.
    Returns comma-separated genre tags, or None when every source answered and none knew the genre.
    Raises GenreLookupFailed when none knew it but Last.fm, Deezer or Claude did not answer.
    Without Claude's genre, sources that agree decide (see _disagree). Sources that disagree raise
    GenreDisputed when `ask_on_dispute` (someone can be asked); otherwise the first of them decides.
    `isrc` is for Deezer; without it the one Spotify gave the track is used.
    """
    title = track.name
    artist = ", ".join(a.name for a in track.artists) if track.artists else ""
    silent: list[str] = []  # sources that did not answer
    # What each source knew, by (service, what it looked at), in order of precedence: without Claude,
    # the first one decides a tie between agreeing sources and a disagreement nobody settles
    found: dict[tuple[str, str], str] = {}

    # 1. Last.fm — returns multiple tags
    track_tags = await get_track_tags(title, artist)
    if track_tags is None:
        silent.append("Last.fm")
    elif genre := _classifiable(track_tags):
        found[("Last.fm", "теги трека")] = genre
    else:
        # 2. Last.fm tags of the track's artists
        genre, answered = await _resolve_lastfm_artists([a.name for a in track.artists or []])
        if genre:
            found[("Last.fm", "теги исполнителя")] = genre
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
        found[("Deezer", "жанр релиза")] = genre

    # Claude decides, with what the sources found
    try:
        genre = await _resolve_ai(title, artist, found)
    except Exception as e:
        log.warning(f"AI genre classification failed for '{title}': {e}")
        silent.append("AI")
    else:
        if genre:
            log.debug(f"Genre via Claude for '{title}': {genre} (hints: {_found_text(found)})")
            return genre

    # Without Claude's answer, sources that agree decide by themselves
    if found:
        named = [set(classify_track_multi(tags)) for tags in found.values()]  # each source's playlists
        if not _disagree(named):
            genre = _together(list(found.values()), named)
            agreed = "Sources agree on" if len(found) > 1 else "One source knew"
            log.debug(f"{agreed} the genre of '{title}': {genre} ({_found_text(found)})")
            return genre
        # A disagreement is for a person to settle when one can be asked. Until then, or for callers
        # that ask nobody, the first source decides.
        (first_service, _), first = next(iter(found.items()))
        if ask_on_dispute:
            log.debug(f"Sources disagree on the genre of '{title}', for a person to settle: {_found_text(found)}")
            raise GenreDisputed(
                choices=[tag for playlist, tag in PICK_TAGS.items() if any(playlist in p for p in named)],
                fallback=first,
                sources=[service for service, _ in found],  # Last.fm is found once: its track or its artists
            )
        log.debug(f"Sources disagree on the genre of '{title}', {first_service} decides: {first}")
        return first

    if silent:
        raise GenreLookupFailed(f"no genre for '{title}': {' and '.join(silent)} did not answer")

    log.debug(f"No genre found for '{title}' by '{artist}'")
    return None


def _found_text(found: dict[tuple[str, str], str], describe: Callable[[str], str] = str) -> str:
    """What the sources found, for the prompt and the logs: «Last.fm, теги трека: hip hop; …»."""
    return "; ".join(f"{service}, {what}: {describe(genre)}" for (service, what), genre in found.items())


def _says(playlists: set[str], playlist: str) -> bool:
    """Whether a source leading to `playlists` says `playlist` too.

    It does with the same playlist, or a broader or more exact one by the hierarchy (Rock for Metal,
    DnB for Electronic).
    """
    return any(
        other == playlist or suppressing_genre(other, [playlist]) or suppressing_genre(playlist, [other])
        for other in playlists
    )


def _disagree(named: list[set[str]]) -> bool:
    """Whether two sources each name a playlist the other does not say (see _says).

    Hip-Hop against Rock is a disagreement. One source naming more («hip hop, electronic» against
    «electronic»), or making the other's genre exact (Metal against Rock), is not.
    """
    return any(
        any(not _says(b, p) for p in a) and any(not _says(a, p) for p in b)
        for a, b in itertools.combinations(named, 2)
    )


def _together(genres: list[str], named: list[set[str]]) -> str:
    """The genre of agreeing sources.

    A source's own genre when it alone leads to every playlist they lead to together, else all their tags.
    «electronic, hip hop» and «drum and bass» give all three tags, which lead to Hip-Hop and DnB.
    """
    together = set(classify_track_multi(", ".join(genres)))
    for source_genre, playlists in zip(genres, named):
        if playlists == together:
            return source_genre
    return ", ".join(dict.fromkeys(tag for tags in genres for tag in tags.split(", ")))


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


async def _resolve_ai(title: str, artist: str, hints: dict[tuple[str, str], str]) -> str | None:
    """Ask Claude to pick the track's TURDOM genres, with what other sources found as hints.

    None when Claude does not know or cannot be used at all; raises when it did not answer.
    """
    parts = [f"Определи жанр трека «{title}» исполнителя «{artist}»."]
    if hints:
        known = _found_text(hints, _hint)
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


async def resolve_and_save_genre(pool: asyncpg.Pool, track, isrc: str | None = None, *,
                                 ask_on_dispute: bool = False) -> str | None:
    """Make sure the track has a genre: return the saved one, or resolve and save it.

    A genre the track already has, resolved earlier or picked by a person, is kept
    and not looked up again; one it got during the lookup stays too, and is what comes back.
    With no row to save to, the genre found comes back (on a dispute, the first source's).
    None means every source answered, nobody knows the genre and the track still has none;
    GenreLookupFailed from resolve_genre passes through when a source did not answer and the track
    still has no genre.
    With `ask_on_dispute`, GenreDisputed passes through too, after the first source's genre is saved
    for the track to keep until a person picks, even when nobody can be asked or nobody answers. When
    saving it fails, the dispute still passes through, with `kept` None. Without the flag, the first
    source's genre is simply the one found.
    When the read after the lookup fails, it is logged and what the lookup gave stands: its genre (on a
    dispute whose first genre updated no row, the first source's), None, or GenreLookupFailed.
    `isrc` is the one the caller already found, for Deezer.
    """
    if saved := await _track_genre_by_spotify_id(pool, track.id):
        return saved

    try:
        genre = await resolve_genre(track, isrc, ask_on_dispute=ask_on_dispute)
    except GenreLookupFailed:
        if now := await _genre_after_lookup(pool, track, None):
            return now  # it got a genre during the lookup, so the silent source no longer matters
        raise
    except GenreDisputed as dispute:
        try:
            stored = await _save_found_genre(pool, track, dispute.fallback)
        except Exception as e:  # the question still goes out, and the pick gives the track its genre
            log.warning(f"Failed to keep the first source's genre for '{track.name}': {e}")
            raise dispute from None
        if stored:
            dispute.kept = dispute.fallback
            raise dispute
        genre = dispute.fallback  # nothing is left to settle: the track got a genre meanwhile, or has no row
    else:
        if genre and await _save_found_genre(pool, track, genre):
            return genre
    # Nothing found, or nothing updated: a genre the track got during the lookup stays and comes back.
    # Without one, the genre found does (None when there is none), even with no row to save it to.
    return await _genre_after_lookup(pool, track, genre)


async def _genre_after_lookup(pool: asyncpg.Pool, track, found: str | None) -> str | None:
    """The genre the track got during the lookup, else `found`.

    A failed read gives `found` too: at worst the watcher asks about a track whose genre someone has just picked,
    and «Сохранить» then says it is already picked.
    """
    try:
        return await _track_genre_by_spotify_id(pool, track.id) or found
    except Exception as e:
        log.warning(f"Failed to read the genre of '{track.name}' again: {e}")
        return found


async def _track_genre_by_spotify_id(pool: asyncpg.Pool, spotify_track_id: str) -> str | None:
    """The genre the track with this Spotify ID has now, or None."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT genre FROM tracks WHERE spotify_track_id = $1", spotify_track_id)


async def _save_found_genre(pool: asyncpg.Pool, track, genre: str) -> bool:
    """Save the genre the bot found for a track that has none.

    False when nothing was updated: the track got a genre meanwhile, or there is no such track.
    """
    async with pool.acquire() as conn:
        saved_id = await conn.fetchval(_SAVE_FOUND_GENRE, genre, track.id)
    if saved_id is None:
        return False
    log.debug(f"Genre for '{track.name}': {genre}")
    return True


async def get_track_genre(pool: asyncpg.Pool, track_db_id: int) -> str | None:
    """The genre a track has now, by its id in the database, or None."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT genre FROM tracks WHERE id = $1", track_db_id)


async def save_picked_genre(pool: asyncpg.Pool, track_db_id: int, genre: str) -> bool:
    """Save a genre a person picked. True if it was saved.

    It replaces a genre the bot found itself, but never a pick already made (by anyone, in any question).
    genre_approved marks it as a person's pick, which /backfill_genres reset keeps.
    """
    async with pool.acquire() as conn:
        saved_id = await conn.fetchval(
            "UPDATE tracks SET genre = $1, genre_approved = TRUE WHERE id = $2 AND genre_approved IS NOT TRUE "
            "RETURNING id",
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
                    # Nothing comes back when the track got a genre while the run went on (a pick, the watcher)
                    if await conn.fetchval(_SAVE_FOUND_GENRE, genre, track_id) is not None:
                        resolved += 1
            except Exception as e:
                log.warning(f"Failed to backfill genre for {track_id}: {e}")
            processed += 1
            await asyncio.sleep(0.25)  # after a failure too: a rate-limited Last.fm needs the pause most

    unknown = processed - resolved
    log.info(f"Genre backfill done: {processed} processed, {resolved} resolved, {unknown} unknown")
    return {"processed": processed, "resolved": resolved, "unknown": unknown}
