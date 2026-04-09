"""Last.fm API client — track-level genre tags."""

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://ws.audioscrobbler.com/2.0/"
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


async def get_track_tags(title: str, artist: str) -> list[str]:
    """Fetch top tags for a track from Last.fm.

    Returns list of tag names (lowercased), ordered by popularity.
    Empty list if track not found or API unavailable.
    """
    if not settings.lastfm_api_key:
        return []

    params = {
        "method": "track.getTopTags",
        "track": title,
        "artist": artist,
        "api_key": settings.lastfm_api_key,
        "format": "json",
    }

    try:
        resp = await _get_client().get(_BASE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        # Last.fm returns some errors as 200 with {"error": ...}
        if "error" in data:
            log.warning(f"Last.fm API error {data['error']}: {data.get('message')}")
            return []

        tags = data.get("toptags", {}).get("tag", [])
        if not tags:
            return []

        # Filter low-count tags and return names
        result = []
        for tag in tags:
            name = tag.get("name", "").strip().lower()
            count = int(tag.get("count", 0))
            if name and count >= 20:
                result.append(name)

        log.debug(f"Last.fm tags for '{title}' by '{artist}': {result[:5]}")
        return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return []
        log.warning(f"Last.fm API error for '{title}': {e}")
        return []
    except Exception as e:
        log.warning(f"Last.fm request failed for '{title}': {e}")
        return []
