"""After a session ends, distribute kept tracks to genre playlists."""
import logging

import asyncpg

from app.spotify.auth import get_spotify

log = logging.getLogger(__name__)

# Genre playlist names → Spotify IDs (loaded from DB at runtime)
_genre_playlist_ids: dict[str, str] = {}

GENRE_MAP = {
    "TURDOM Electronic": ["edm", "house", "electronic", "electro", "techno", "trance", "stutter", "big room", "darkwave", "breakbeat", "uk garage", "dub", "big beat", "synthwave", "hardstyle", "eurodance", "frenchcore", "melbourne bounce", "new rave", "gabber", "speedcore", "tekno", "ebm", "cold wave", "new wave", "italo dance", "disco", "hi-nrg", "nu disco", "moombahton", "breakcore", "glitch", "vaporwave", "nightcore", "amapiano", "afro tech"],
    "TURDOM Pop": ["pop", "latin", "reggaeton", "corrido", "sierreño", "banda", "ranchera", "mariachi", "norteño", "grupera", "bachata", "dembow", "techengue", "música mexicana", "urbano latino", "anime", "j-rock", "j-pop", "japanese", "j-dance", "visual kei", "shibuya-kei", "vocaloid"],
    "TURDOM Metal": ["metal", "metalcore", "deathcore", "djent", "hardcore", "screamo", "post-hardcore", "mathcore", "industrial"],
    "TURDOM Rock": ["rock", "grunge", "punk", "psychobilly", "emo"],
    "TURDOM Hip-Hop": ["hip hop", "rap", "trap", "horrorcore", "drill", "grime", "boom bap", "bounce", "uk drill", "aussie drill", "sexy drill"],
    "TURDOM Indie": ["indie", "singer-songwriter", "anti-folk", "madchester", "alternative dance", "shoegaze", "dream", "chillwave", "folk", "country", "americana", "celtic", "medieval"],
    "TURDOM R&B": ["r&b", "soul", "funk", "neo soul", "motown", "doo-wop", "quiet storm", "new jack swing"],
    "TURDOM DnB": ["drum and bass", "dubstep", "riddim", "drumstep", "deathstep", "bass music", "bass house", "future bass", "liquid funk", "jungle", "3 step"],
    "TURDOM Chill": ["downtempo", "trip hop", "lo-fi", "chill", "ambient", "dark ambient", "drone", "jazz", "swing music", "big band", "nu jazz"],
    "TURDOM Soundtrack": ["soundtrack", "score", "game", "classical", "concerto", "piano"],
    "TURDOM Phonk": ["phonk"],
}


def classify_track(genre_str: str) -> str | None:
    """Classify a track's genre string into a genre playlist name.

    Matches by whole words: keyword must appear as complete word(s) in the genre.
    Longer keyword matches take priority to avoid ambiguity
    (e.g. 'hardcore hip hop' → Hip-Hop via 'hip hop' (2 words) over 'hardcore' (1 word)).
    """
    genres = [g.strip().lower() for g in genre_str.split(", ")]

    best_playlist = None
    best_kw_words = 0

    for genre in genres:
        genre_words = set(genre.split())
        for playlist_name, keywords in GENRE_MAP.items():
            for kw in keywords:
                kw_words = kw.split()
                if set(kw_words).issubset(genre_words) and len(kw_words) > best_kw_words:
                    best_kw_words = len(kw_words)
                    best_playlist = playlist_name

    return best_playlist


async def load_genre_playlist_ids(pool: asyncpg.Pool):
    """Load genre playlist Spotify IDs from user's playlists."""
    global _genre_playlist_ids
    sp = await get_spotify()
    user = await sp.current_user()

    offset = 0
    while True:
        pls = await sp.playlists(user.id, limit=50, offset=offset)
        if not pls.items:
            break
        for pl in pls.items:
            if pl.name in GENRE_MAP:
                _genre_playlist_ids[pl.name] = pl.id
        offset += len(pls.items)
        if offset >= pls.total:
            break

    log.info(f"Loaded {len(_genre_playlist_ids)} genre playlists")


async def distribute_session_tracks(pool: asyncpg.Pool, session_id: int):
    """Distribute kept tracks from a finished session to genre playlists."""
    if not _genre_playlist_ids:
        await load_genre_playlist_ids(pool)

    if not _genre_playlist_ids:
        log.warning("No genre playlists found — skipping distribution")
        return {"distributed": 0, "skipped": 0}

    sp = await get_spotify()

    # Get kept tracks with genres
    async with pool.acquire() as conn:
        tracks = await conn.fetch("""
            SELECT st.spotify_track_id, pt.genre
            FROM session_tracks st
            LEFT JOIN playlist_tracks pt ON st.spotify_track_id = pt.spotify_track_id
            WHERE st.session_id = $1 AND st.vote_result = 'keep'
            AND pt.genre IS NOT NULL AND pt.genre != 'unknown'
        """, session_id)

    # Group by genre playlist
    to_add: dict[str, list[str]] = {}
    skipped = 0

    for track in tracks:
        genre_playlist = classify_track(track["genre"])
        if genre_playlist and genre_playlist in _genre_playlist_ids:
            if genre_playlist not in to_add:
                to_add[genre_playlist] = []
            to_add[genre_playlist].append(track["spotify_track_id"])
        else:
            skipped += 1

    # Add tracks to genre playlists
    distributed = 0
    for playlist_name, track_ids in to_add.items():
        spotify_playlist_id = _genre_playlist_ids[playlist_name]
        uris = [f"spotify:track:{tid}" for tid in track_ids]

        # Check for existing tracks to avoid duplicates
        try:
            existing = set()
            offset = 0
            while True:
                items = await sp.playlist_items(spotify_playlist_id, limit=100, offset=offset)
                for item in items.items:
                    if item.track:
                        existing.add(item.track.id)
                offset += len(items.items)
                if offset >= items.total:
                    break

            new_uris = [uri for uri, tid in zip(uris, track_ids) if tid not in existing]

            if new_uris:
                for i in range(0, len(new_uris), 100):
                    await sp.playlist_add(spotify_playlist_id, new_uris[i:i + 100])
                distributed += len(new_uris)
                log.info(f"Added {len(new_uris)} tracks to {playlist_name}")
        except Exception as e:
            log.error(f"Failed to add tracks to {playlist_name}: {e}")

    log.info(f"Distribution done: {distributed} distributed, {skipped} skipped")
    return {"distributed": distributed, "skipped": skipped}
