"""Deezer API client — the genre a music distributor gave a track's release, found by ISRC. No key needed.

The genre is set per release when the label or distributor (DistroKid and the like) uploads it, so even
little-known tracks have one. It is broad and per album, not per track: a hint for Claude and a fallback.
"""

import logging
import re

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.deezer.com"
_NO_DATA = 800  # no such track or album: an answer like any other
_client: httpx.AsyncClient | None = None

# Deezer genre id (stable; its name is localized by the caller's country) → a tag the genre playlists
# understand. Alternative (85) and «Musique russe» (544) name no style, and Reggae, Blues, Kids or the
# regional genres have no TURDOM playlist: those decide nothing. Albums also carry ids missing from
# Deezer's /genre menu, such as 84, 87, 111, 122 and 174.
_GENRE_TAGS = {
    132: "pop",
    116: "hip hop",
    152: "rock",
    113: "electronic",  # Dance
    106: "electronic",  # Electro
    111: "electronic",  # Techno/House
    165: "r&b",
    169: "soul",  # Soul & Funk
    464: "metal",
    466: "folk",
    84: "country",
    87: "indie rock",  # the tag Last.fm gives the same style, so both land in the same playlist
    129: "jazz",
    98: "classical",
    173: "soundtrack",  # Films/Games
    174: "soundtrack",  # Film Scores
    197: "latin",
    122: "reggaeton",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


async def _get(path: str, what: str) -> dict | None:
    """Deezer's answer as a dict; {} when it knows no such thing, None when it did not answer."""
    response = await _get_client().get(f"{_BASE_URL}{path}")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        log.warning(f"Deezer answered {what} with {type(data).__name__}")
        return None
    error = data.get("error")
    if not error:
        return data
    if isinstance(error, dict) and error.get("code") == _NO_DATA:
        log.debug(f"Deezer does not know {what}")
        return {}
    log.warning(f"Deezer error for {what}: {error}")
    return None


def _artist_id(item: dict) -> int | None:
    artist = item.get("artist")
    return artist.get("id") if isinstance(artist, dict) else None


def _clean_isrc(isrc: object) -> str | None:
    """The ISRC as Deezer finds it (12 letters and digits), or None for anything else."""
    cleaned = re.sub(r"[^A-Z0-9]", "", str(isrc or "").upper())
    return cleaned if len(cleaned) == 12 else None


async def get_release_genre_tags(isrc: str | None) -> list[str] | None:
    """Tags for the genres of the release that holds the track with this ISRC.

    Empty list when there is no ISRC, Deezer does not know the track, the release is a
    compilation or someone else's (a soundtrack, a label sampler), or its genres name no
    TURDOM style. None when Deezer did not answer.
    """
    isrc = _clean_isrc(isrc)
    if not isrc:
        return []
    what = f"ISRC {isrc}"
    try:
        track = await _get(f"/track/isrc:{isrc}", what)
        if track is None:
            return None
        track_album = track.get("album")
        album_id = track_album.get("id") if isinstance(track_album, dict) else None
        if not isinstance(album_id, int):
            album_id = None
        album = await _get(f"/album/{album_id}", f"album {album_id} of {what}") if album_id else {}
        if album is None:
            return None
        if not album:
            return []
        # A soundtrack or a sampler that also holds the track says nothing about its style
        artist_id = _artist_id(track)
        if album.get("record_type") == "compile" or artist_id is None or _artist_id(album) != artist_id:
            log.debug(f"Deezer gives {what} someone else's release '{album.get('title')}'")
            return []
        genres = album.get("genres")
        listed = genres.get("data") if isinstance(genres, dict) else None
        tags = []
        for genre in listed if isinstance(listed, list) else []:
            genre_id = genre.get("id") if isinstance(genre, dict) else None
            tag = _GENRE_TAGS.get(genre_id) if isinstance(genre_id, int) else None
            if tag and tag not in tags:
                tags.append(tag)
        return tags
    except Exception as e:
        log.warning(f"Deezer request failed for {what}: {e}")
        return None
