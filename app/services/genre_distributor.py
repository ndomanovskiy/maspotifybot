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

# Hierarchy: specific genre suppresses its parent.
# If a track matches Metal, don't also add to Rock.
_GENRE_HIERARCHY = {
    "TURDOM Metal": {"TURDOM Rock"},       # metal suppresses rock
    "TURDOM DnB": {"TURDOM Electronic"},   # dnb suppresses electronic
    "TURDOM R&B": {"TURDOM Pop"},           # r&b suppresses pop
}


def classify_track(genre_str: str) -> str | None:
    """Classify a track's genre string into a single best genre playlist name.

    Kept for backward compatibility (stats, tests).
    """
    results = classify_track_multi(genre_str)
    return results[0] if results else None


def classify_track_multi(genre_str: str) -> list[str]:
    """Classify a track's genre string into ALL matching genre playlists.

    Applies hierarchy rules: Metal suppresses Rock, DnB suppresses Electronic, etc.
    Returns deduplicated list of TURDOM playlist names.
    """
    genres = [g.strip().lower() for g in genre_str.split(", ")]

    matched: set[str] = set()

    for genre in genres:
        genre_words = set(genre.split())
        best_playlist = None
        best_kw_words = 0

        for playlist_name, keywords in GENRE_MAP.items():
            for kw in keywords:
                kw_words = kw.split()
                if set(kw_words).issubset(genre_words) and len(kw_words) > best_kw_words:
                    best_kw_words = len(kw_words)
                    best_playlist = playlist_name

        if best_playlist:
            matched.add(best_playlist)

    # Apply hierarchy: remove suppressed genres
    # Each individual tag maps to exactly one playlist (longest keyword match wins)
    suppressed = set()
    for specific, parents in _GENRE_HIERARCHY.items():
        if specific in matched:
            suppressed |= parents

    result = [p for p in matched if p not in suppressed]

    if matched and not result:
        log.warning(f"All genres suppressed for tags: {genre_str} (matched: {matched})")

    return sorted(result)


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
    """Distribute kept tracks from a finished session to genre playlists.

    Uses classify_track_multi — a track can go to multiple playlists.
    """
    if not _genre_playlist_ids:
        await load_genre_playlist_ids(pool)

    if not _genre_playlist_ids:
        log.warning("No genre playlists found — skipping distribution")
        return {"distributed": 0, "skipped": 0}

    sp = await get_spotify()

    # Get kept tracks with genres
    async with pool.acquire() as conn:
        tracks = await conn.fetch("""
            SELECT st.spotify_track_id, t.genre
            FROM session_tracks st
            JOIN tracks t ON st.track_id = t.id
            WHERE st.session_id = $1 AND st.vote_result = 'keep'
            AND t.genre IS NOT NULL AND t.genre != 'unknown'
        """, session_id)

    # Group by genre playlist (multi-genre: one track → multiple playlists)
    to_add: dict[str, list[str]] = {}
    skipped = 0

    for track in tracks:
        playlists = classify_track_multi(track["genre"])
        if playlists:
            for pl_name in playlists:
                if pl_name in _genre_playlist_ids:
                    if pl_name not in to_add:
                        to_add[pl_name] = []
                    to_add[pl_name].append(track["spotify_track_id"])
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


async def check_previously_dropped(pool: asyncpg.Pool, spotify_track_id: str) -> list[dict] | None:
    """Check if a track was previously dropped in any session.

    Returns list of sessions where it was dropped, or None if never dropped.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT s.playlist_name, s.started_at,
                   COALESCE('@' || NULLIF(u.telegram_username, ''), u.telegram_name, '?') as added_by
            FROM session_tracks st
            JOIN sessions s ON st.session_id = s.id
            LEFT JOIN users u ON st.added_by_spotify_id = u.spotify_id
            WHERE st.spotify_track_id = $1 AND st.vote_result = 'drop'
        """, spotify_track_id)

    if not rows:
        return None

    return [{"playlist": r["playlist_name"],
             "date": r["started_at"].strftime("%d/%m/%Y") if r["started_at"] else "?",
             "added_by": r["added_by"]} for r in rows]
