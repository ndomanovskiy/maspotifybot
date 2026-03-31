"""
One-time script: fetch genres for all tracks via Spotify artist genres,
save to DB, and print distribution.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import Counter
import asyncpg
from app.config import settings
from app.spotify.auth import load_token_from_db, get_spotify


async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=5)

    # Ensure genre column exists
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS genre TEXT")

    # Load Spotify auth
    await load_token_from_db(pool)
    sp = await get_spotify()

    # Get all unique tracks without genre
    async with pool.acquire() as conn:
        tracks = await conn.fetch("""
            SELECT DISTINCT spotify_track_id, title, artist
            FROM playlist_tracks
            WHERE genre IS NULL
        """)

    print(f"Tracks without genre: {len(tracks)}")

    # Spotify allows batch artist lookup — 50 at a time
    # First, get artist IDs for each track
    artist_genre_cache = {}  # artist_id -> genres list
    batch_size = 50
    processed = 0

    for track in tracks:
        track_id = track["spotify_track_id"]

        try:
            t = await sp.track(track_id)
            if not t.artists:
                continue

            # Get primary artist
            artist_id = t.artists[0].id

            if artist_id not in artist_genre_cache:
                artist = await sp.artist(artist_id)
                artist_genre_cache[artist_id] = artist.genres or []

            genres = artist_genre_cache[artist_id]
            genre_str = ", ".join(genres) if genres else "unknown"

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE playlist_tracks SET genre = $1 WHERE spotify_track_id = $2",
                    genre_str, track_id,
                )

            processed += 1
            if processed % 50 == 0:
                print(f"Processed {processed}/{len(tracks)}...")

        except Exception as e:
            print(f"Error for {track['title']}: {e}")
            # Rate limit — wait
            if "429" in str(e):
                print("Rate limited, waiting 30 sec...")
                await asyncio.sleep(30)
            continue

    print(f"\nDone! Processed {processed} tracks")

    # Print genre distribution
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT genre FROM playlist_tracks WHERE genre IS NOT NULL AND genre != 'unknown'")

    # Split multi-genre strings and count
    genre_counter = Counter()
    for row in rows:
        for g in row["genre"].split(", "):
            genre_counter[g.strip()] += 1

    print(f"\n=== GENRE DISTRIBUTION ({len(genre_counter)} unique genres) ===\n")
    for genre, count in genre_counter.most_common(50):
        bar = "█" * (count // 10)
        print(f"{count:4d} {bar} {genre}")

    print(f"\n=== TOP-LEVEL CATEGORIES ===\n")

    # Group into high-level categories
    categories = {
        "Rock": ["rock", "metal", "punk", "grunge", "emo", "hardcore", "alternative"],
        "Pop": ["pop"],
        "Electronic": ["house", "electronic", "edm", "techno", "trance", "dubstep", "drum and bass", "dnb", "electro"],
        "Hip-Hop/Rap": ["hip hop", "rap", "trap"],
        "R&B/Soul": ["r&b", "soul", "neo soul", "funk"],
        "Jazz": ["jazz"],
        "Classical": ["classical", "orchestra"],
        "Indie": ["indie"],
        "Country": ["country"],
        "Latin": ["latin", "reggaeton"],
        "Soundtrack": ["soundtrack", "score", "game"],
    }

    cat_counter = Counter()
    uncategorized = Counter()

    for genre, count in genre_counter.items():
        found = False
        for cat, keywords in categories.items():
            if any(kw in genre.lower() for kw in keywords):
                cat_counter[cat] += count
                found = True
                break
        if not found:
            uncategorized[genre] += count

    for cat, count in cat_counter.most_common():
        bar = "█" * (count // 20)
        print(f"{count:4d} {bar} {cat}")

    print(f"\nUncategorized genres ({len(uncategorized)}):")
    for genre, count in uncategorized.most_common(20):
        print(f"  {count:4d} {genre}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
