"""Last.fm API client — genre tags of a track or an artist."""

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://ws.audioscrobbler.com/2.0/"
_client: httpx.AsyncClient | None = None

# Last.fm error codes, https://www.last.fm/api/errorcodes
_NOT_FOUND = 6  # no such track or artist: an answer like any other
_KEY_REFUSED = {4, 10, 26}  # failed authentication, invalid or suspended key: out until someone fixes the key


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


async def get_track_tags(title: str, artist: str) -> list[str] | None:
    """Fetch top tags for a track from Last.fm.

    Returns list of tag names (lowercased), ordered by popularity. Empty list if Last.fm
    has no tags or does not know the track, or cannot be used at all (no key, a refused key).
    None if Last.fm did not answer.
    """
    return await _top_tags({"method": "track.getTopTags", "track": title, "artist": artist}, f"'{title}' by '{artist}'")


async def get_artist_tags(artist: str) -> list[str] | None:
    """Fetch top tags for an artist from Last.fm, the same way as get_track_tags."""
    return await _top_tags({"method": "artist.getTopTags", "artist": artist}, f"artist '{artist}'")


def _error_answer(code: object, message: object, what: str) -> list[str] | None:
    """What a Last.fm error code means: [] when Last.fm knows nothing or cannot be used, None when it did not answer."""
    if code == _NOT_FOUND:
        log.debug(f"Last.fm does not know {what}")
        return []
    if code in _KEY_REFUSED:
        log.warning(f"Last.fm refuses the API key (error {code}), going on without Last.fm")
        return []
    log.warning(f"Last.fm API error {code} for {what}: {message}")
    return None


def _error_body(response: httpx.Response) -> dict:
    """Last.fm puts its error code in the body of a 4xx answer too."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _top_tags(query: dict[str, str], what: str) -> list[str] | None:
    if not settings.lastfm_api_key:
        return []

    params = {**query, "api_key": settings.lastfm_api_key, "format": "json"}

    try:
        resp = await _get_client().get(_BASE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        # Last.fm returns some errors as 200 with {"error": ...}
        if "error" in data:
            return _error_answer(data["error"], data.get("message"), what)

        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):  # a lone tag comes as an object, not a list
            tags = [tags]
        if not tags:
            return []

        # Filter low-count tags and return names
        result = []
        for tag in tags:
            name = tag.get("name", "").strip().lower()
            count = int(tag.get("count", 0))
            if name and count >= 20:
                result.append(name)

        log.debug(f"Last.fm tags for {what}: {result[:5]}")
        return result

    except httpx.HTTPStatusError as e:
        body = _error_body(e.response)
        if "error" in body:
            return _error_answer(body["error"], body.get("message"), what)
        if e.response.status_code in (400, 404):  # an invalid request (say, no artist) is not Last.fm being down
            return []
        # Only the status: the error's text holds the request URL, API key included
        log.warning(f"Last.fm API error for {what}: HTTP {e.response.status_code}")
        return None
    except Exception as e:
        log.warning(f"Last.fm request failed for {what}: {e}")
        return None
