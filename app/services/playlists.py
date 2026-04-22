import re
import logging

import asyncpg

from app.spotify.auth import get_spotify
from app.services.genre_resolver import resolve_and_save_genre
from app.services.normalize import normalize_title, normalize_artist, is_fuzzy_match, base_title, has_version_marker

log = logging.getLogger(__name__)


async def import_playlist(pool: asyncpg.Pool, playlist_spotify_id: str) -> dict:
    """Import a single playlist and all its tracks into the database."""
    sp = await get_spotify()
    pl = await sp.playlist(playlist_spotify_id)

    name = pl.name
    number = _parse_turdom_number(name)
    is_thematic = number is None or _is_thematic(name)
    url = f"https://open.spotify.com/playlist/{playlist_spotify_id}"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO playlists (spotify_id, name, number, url, status, is_thematic, track_count)
            VALUES ($1, $2, $3, $4, 'listened', $5, $6)
            ON CONFLICT (spotify_id) DO UPDATE
            SET name = $2, number = $3, track_count = $6
            RETURNING id
            """,
            playlist_spotify_id, name, number, url, is_thematic, pl.tracks.total,
        )
        playlist_db_id = row["id"]

    # Import all tracks (paginated)
    imported = 0
    offset = 0
    while True:
        items = await sp.playlist_items(playlist_spotify_id, limit=100, offset=offset)
        if not items.items:
            break

        async with pool.acquire() as conn:
            for item in items.items:
                if item.track is None:
                    continue
                track = item.track

                # Get ISRC
                isrc = None
                if hasattr(track, "external_ids") and track.external_ids:
                    isrc = getattr(track.external_ids, "isrc", None)

                added_by = item.added_by.id if item.added_by else None
                added_at = item.added_at if hasattr(item, "added_at") else None

                try:
                    title = track.name
                    artist = ", ".join(a.name for a in track.artists)

                    # Upsert into tracks table
                    track_row = await conn.fetchrow(
                        """
                        INSERT INTO tracks (spotify_track_id, title, artist, isrc,
                                            normalized_title, normalized_artist, normalized_base)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (spotify_track_id) DO UPDATE
                        SET title = EXCLUDED.title, artist = EXCLUDED.artist,
                            isrc = COALESCE(EXCLUDED.isrc, tracks.isrc),
                            normalized_title = EXCLUDED.normalized_title,
                            normalized_artist = EXCLUDED.normalized_artist,
                            normalized_base = EXCLUDED.normalized_base
                        RETURNING id
                        """,
                        track.id, title, artist, isrc,
                        normalize_title(title), normalize_artist(artist),
                        base_title(title),
                    )
                    track_db_id = track_row["id"]

                    # Insert into playlist_tracks with FK to tracks
                    await conn.execute(
                        """
                        INSERT INTO playlist_tracks (playlist_id, track_id, spotify_track_id,
                                                     added_by_spotify_id, added_at)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (playlist_id, spotify_track_id) DO UPDATE SET track_id = $2
                        """,
                        playlist_db_id, track_db_id, track.id, added_by, added_at,
                    )
                    imported += 1

                    # Resolve genre from Spotify artist
                    try:
                        await resolve_and_save_genre(pool, track)
                    except Exception as e:
                        log.warning(f"Failed to resolve genre for '{track.name}': {e}")

                except Exception as e:
                    log.warning(f"Failed to import track {track.id}: {e}")

        offset += len(items.items)
        if offset >= items.total:
            break

    log.info(f"Imported playlist '{name}': {imported} tracks")
    return {"name": name, "tracks": imported, "playlist_db_id": playlist_db_id}


async def import_all_turdom(pool: asyncpg.Pool) -> list[dict]:
    """Find all TURDOM playlists from current user and import them."""
    sp = await get_spotify()
    results = []
    offset = 0

    while True:
        playlists = await sp.playlists(await _get_current_user_id(sp), limit=50, offset=offset)
        if not playlists.items:
            break

        for pl in playlists.items:
            if pl.name and "TURDOM" in pl.name.upper():
                log.info(f"Found TURDOM playlist: {pl.name} ({pl.id})")
                result = await import_playlist(pool, pl.id)
                results.append(result)

        offset += len(playlists.items)
        if offset >= playlists.total:
            break

    return results


async def check_duplicate(pool: asyncpg.Pool, spotify_track_id: str, isrc: str | None = None,
                          title: str | None = None, artist: str | None = None) -> list[dict]:
    """Check if a track is a duplicate across all imported playlists.

    Returns list of dicts with 'match' type: 'exact', 'isrc', 'fuzzy_exact', 'fuzzy_contains', 'fuzzy_levenshtein'.
    """
    duplicates = []
    async with pool.acquire() as conn:
        # 1. Exact Track ID match
        rows = await conn.fetch(
            """
            SELECT t.spotify_track_id, t.title, t.artist,
                   p.id as playlist_id, p.name as playlist_name, p.url as playlist_url
            FROM playlist_tracks pt
            JOIN tracks t ON pt.track_id = t.id
            JOIN playlists p ON pt.playlist_id = p.id
            WHERE t.spotify_track_id = $1
            """,
            spotify_track_id,
        )
        for r in rows:
            duplicates.append({"match": "exact", "spotify_track_id": r["spotify_track_id"],
                               "title": r["title"], "artist": r["artist"],
                               "playlist": r["playlist_name"], "playlist_id": r["playlist_id"],
                               "url": r["playlist_url"]})

        # 2. ISRC match (same song, different album)
        if isrc and not duplicates:
            rows = await conn.fetch(
                """
                SELECT t.spotify_track_id, t.title, t.artist,
                       p.id as playlist_id, p.name as playlist_name, p.url as playlist_url
                FROM playlist_tracks pt
                JOIN tracks t ON pt.track_id = t.id
                JOIN playlists p ON pt.playlist_id = p.id
                WHERE t.isrc = $1 AND t.spotify_track_id != $2
                """,
                isrc, spotify_track_id,
            )
            for r in rows:
                duplicates.append({"match": "isrc", "spotify_track_id": r["spotify_track_id"],
                                   "title": r["title"], "artist": r["artist"],
                                   "playlist": r["playlist_name"], "playlist_id": r["playlist_id"],
                                   "url": r["playlist_url"]})

        # 3. Fuzzy match (normalized title + artist)
        if not duplicates and title and artist:
            norm_title = normalize_title(title)
            norm_artist = normalize_artist(artist)

            # Find candidates with same normalized artist
            candidates = await conn.fetch(
                """
                SELECT t.spotify_track_id, t.title, t.artist,
                       t.normalized_title, t.normalized_artist,
                       p.id as playlist_id, p.name as playlist_name, p.url as playlist_url
                FROM playlist_tracks pt
                JOIN tracks t ON pt.track_id = t.id
                JOIN playlists p ON pt.playlist_id = p.id
                WHERE t.normalized_artist = $1 AND t.spotify_track_id != $2
                """,
                norm_artist, spotify_track_id,
            )

            for r in candidates:
                cand_norm_title = r["normalized_title"] or normalize_title(r["title"])
                match_type = is_fuzzy_match(norm_title, cand_norm_title, norm_artist, norm_artist)
                if match_type:
                    duplicates.append({
                        "match": match_type, "spotify_track_id": r["spotify_track_id"],
                        "title": r["title"], "artist": r["artist"],
                        "playlist": r["playlist_name"], "playlist_id": r["playlist_id"],
                        "url": r["playlist_url"],
                    })

    return duplicates


async def classify_duplicates(pool: asyncpg.Pool, duplicates: list[dict]) -> list[dict]:
    """Enrich duplicates with session history to classify them.

    For each duplicate, checks whether the track was played in a session
    for that playlist. Uses both spotify_track_id and ISRC to handle
    same-song-different-release cases.

    session_status values (match vote_result from DB):
      - 'keep'    → was on session and voted keep (real duplicate)
      - 'drop'    → was on session and voted drop (second chance candidate)
      - 'phantom' → never played in a session (stale DB record)
      - 'pending' → session exists but vote not finalized
    """
    if not duplicates:
        return duplicates

    async with pool.acquire() as conn:
        for d in duplicates:
            playlist_id = d["playlist_id"]
            track_id = d["spotify_track_id"]  # must be present — added in check_duplicate

            # Search by spotify_track_id first, then by ISRC for cross-release matches
            row = await conn.fetchrow(
                """
                SELECT st.vote_result
                FROM session_tracks st
                JOIN sessions s ON st.session_id = s.id
                WHERE s.playlist_id = $1 AND st.spotify_track_id = $2
                ORDER BY s.ended_at DESC NULLS LAST
                LIMIT 1
                """,
                playlist_id, track_id,
            )

            if row is None and d["match"] == "isrc":
                # ISRC match: the session may have a different spotify_track_id
                # for the same song. Look up by any track with same ISRC.
                row = await conn.fetchrow(
                    """
                    SELECT st.vote_result
                    FROM session_tracks st
                    JOIN sessions s ON st.session_id = s.id
                    JOIN tracks t ON st.track_id = t.id
                    JOIN tracks t2 ON t2.spotify_track_id = $2
                    WHERE s.playlist_id = $1 AND t.isrc = t2.isrc AND t.isrc IS NOT NULL
                    ORDER BY s.ended_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    playlist_id, track_id,
                )

            if row is None:
                d["session_status"] = "phantom"
            else:
                d["session_status"] = row["vote_result"]  # 'keep', 'drop', 'pending'

    return duplicates


async def check_siblings(pool: asyncpg.Pool, spotify_track_id: str,
                          title: str, artist: str | None = None,
                          exclude_playlist_id: int | None = None) -> list[dict]:
    """Find sibling tracks: same base title, but a modified version (remix, sped up, etc.).

    Sibling = matching base_title where at least ONE side has a version marker
    (so the original and a remix flag each other) AND not the same spotify track.
    Artist may differ — remixers often relabel the track. Returns list of:
      {match: 'sibling', title, artist, playlist, playlist_id, url, kind}
    where kind = 'self' (target is a version, candidate is unmarked = original),
                 'other' (target is unmarked, candidate is a version),
                 'both' (both are different versions of the same original).

    exclude_playlist_id: skip siblings inside this playlist (e.g. when the track
    was added to TURDOM #X — don't alert on coexisting original/remix in same playlist).
    """
    target_base = base_title(title)
    if not target_base:
        return []
    target_has_marker = has_version_marker(title)

    # Aggregate per-track: same sibling track may live in multiple playlists.
    # Returns one entry per track, with playlists joined as comma-separated names.
    siblings_by_track: dict[str, dict] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.spotify_track_id, t.title, t.artist, t.normalized_base,
                   p.id as playlist_id, p.name as playlist_name, p.url as playlist_url
            FROM playlist_tracks pt
            JOIN tracks t ON pt.track_id = t.id
            JOIN playlists p ON pt.playlist_id = p.id
            WHERE t.normalized_base = $1 AND t.spotify_track_id != $2
                  AND ($3::int IS NULL OR p.id != $3)
            """,
            target_base, spotify_track_id, exclude_playlist_id,
        )
        for r in rows:
            cand_has_marker = has_version_marker(r["title"])
            # Need at least one side to be a version
            if not target_has_marker and not cand_has_marker:
                continue
            # Skip if the candidate's full normalized title == target's
            # (those would be regular fuzzy matches, not siblings)
            if normalize_title(title) == normalize_title(r["title"]):
                continue
            if target_has_marker and cand_has_marker:
                kind = "both"
            elif target_has_marker:
                kind = "self"
            else:
                kind = "other"
            sid = r["spotify_track_id"]
            if sid in siblings_by_track:
                siblings_by_track[sid]["playlists"].append(r["playlist_name"])
            else:
                siblings_by_track[sid] = {
                    "match": "sibling",
                    "kind": kind,
                    "title": r["title"], "artist": r["artist"],
                    "playlist": r["playlist_name"], "playlist_id": r["playlist_id"],
                    "url": r["playlist_url"],
                    "playlists": [r["playlist_name"]],
                }
    # Collapse 'playlists' list into 'playlist' display string when more than one
    result = []
    for s in siblings_by_track.values():
        if len(s["playlists"]) > 1:
            s["playlist"] = ", ".join(s["playlists"])
        result.append(s)
    return result


async def get_track_isrc(spotify_track_id: str) -> str | None:
    """Fetch ISRC for a track from Spotify."""
    try:
        sp = await get_spotify()
        track = await sp.track(spotify_track_id)
        if track.external_ids:
            return getattr(track.external_ids, "isrc", None)
    except Exception:
        pass
    return None


async def get_next_playlist(pool: asyncpg.Pool) -> dict | None:
    """Get the next upcoming or active playlist."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT name, url, status FROM playlists
            WHERE status IN ('active', 'upcoming')
            ORDER BY
                CASE status WHEN 'active' THEN 0 WHEN 'upcoming' THEN 1 END,
                number DESC NULLS LAST
            LIMIT 1
            """
        )
    if row:
        return {"name": row["name"], "url": row["url"], "status": row["status"]}
    return None


async def create_next_playlist(pool: asyncpg.Pool, theme: str | None = None) -> dict:
    """Create next TURDOM playlist in Spotify and register in DB."""
    sp = await get_spotify()
    user_id = await _get_current_user_id(sp)

    # Get next number
    async with pool.acquire() as conn:
        max_num = await conn.fetchval("SELECT MAX(number) FROM playlists")
    next_number = (max_num or 0) + 1

    # Calculate next Wednesday
    from datetime import datetime, timedelta
    today = datetime.now()
    days_until_wed = (2 - today.weekday()) % 7
    if days_until_wed == 0:
        days_until_wed = 7  # next Wednesday, not today
    next_wed = today + timedelta(days=days_until_wed)
    date_str = next_wed.strftime("%d/%m/%Y")

    # Build name
    if theme:
        name = f"TURDOM#{next_number} {date_str} - {theme}"
        is_thematic = True
    else:
        name = f"TURDOM#{next_number} {date_str}"
        is_thematic = False

    # Create in Spotify
    pl = await sp.playlist_create(user_id, name, public=False, description="TURDOM listening session")
    # Make collaborative (tekore 6.x removed collaborative param from playlist_create)
    await sp.playlist_change_details(pl.id, collaborative=True)
    playlist_spotify_id = pl.id
    url = f"https://open.spotify.com/playlist/{playlist_spotify_id}"

    # Register in DB and close previous open playlists (atomic)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Close any active/upcoming playlists (lifecycle: new created = old is done)
            closed = await conn.fetch(
                "UPDATE playlists SET status = 'listened' WHERE status IN ('active', 'upcoming') RETURNING id, number, name",
            )
            for row in closed:
                log.info(f"Auto-closed playlist: {row['name']} (TURDOM#{row['number']})")

            await conn.execute(
                """
                INSERT INTO playlists (spotify_id, name, number, url, status, is_thematic, track_count)
                VALUES ($1, $2, $3, $4, 'upcoming', $5, 0)
                """,
                playlist_spotify_id, name, next_number, url, is_thematic,
            )

    auto_closed = [{"id": r["id"], "number": r["number"], "name": r["name"]} for r in closed]
    log.info(f"Created playlist: {name} ({playlist_spotify_id})")
    return {"name": name, "number": next_number, "url": url, "spotify_id": playlist_spotify_id,
            "auto_closed": auto_closed}


async def reschedule_playlist(pool: asyncpg.Pool, new_date: str) -> dict | None:
    """Reschedule the upcoming playlist to a new date."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, number, spotify_id FROM playlists WHERE status = 'upcoming' ORDER BY number DESC LIMIT 1"
        )
    if not row:
        return None

    # Parse and rebuild name with new date
    old_name = row["name"]
    number = row["number"]

    # Replace date in name
    new_name = re.sub(r"\d{2}/\d{2}/\d{4}", new_date, old_name)
    if new_name == old_name:
        # No date found, append
        new_name = f"TURDOM#{number} {new_date}"

    # Update in DB
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE playlists SET name = $1 WHERE id = $2",
            new_name, row["id"],
        )

    # Update in Spotify
    sp = await get_spotify()
    await sp.playlist_change_details(row["spotify_id"], name=new_name)

    log.info(f"Rescheduled playlist: {old_name} -> {new_name}")
    return {"old_name": old_name, "new_name": new_name}


async def _get_current_user_id(sp) -> str:
    user = await sp.current_user()
    return user.id


def _parse_turdom_number(name: str) -> int | None:
    """Extract number from 'TURDOM#91 ...' or 'TURDOM 91' -> 91."""
    match = re.search(r"TURDOM[#\s]*(\d+)", name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _is_thematic(name: str) -> bool:
    """Check if playlist has a theme (text after date or non-standard name).
    'TURDOM#83 24/12/2025 - Happy New Year' -> True
    'TURDOM#91 18/03/2026' -> False
    'TURDOM CHECK' -> False
    """
    # Strip number and date: TURDOM#83 24/12/2025 - Happy New Year
    match = re.search(r"TURDOM[#\s]*\d+\s*\d{2}/\d{2}/\d{4}\s*[-–—]\s*(.+)", name, re.IGNORECASE)
    return bool(match)
