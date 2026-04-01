import asyncpg

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    telegram_name TEXT NOT NULL,
    spotify_id TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    playlist_spotify_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'ended'))
);

CREATE TABLE IF NOT EXISTS session_tracks (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    spotify_track_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT,
    cover_url TEXT,
    added_by_spotify_id TEXT,
    position INTEGER,
    vote_result TEXT DEFAULT 'pending' CHECK (vote_result IN ('pending', 'keep', 'drop')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS playlists (
    id SERIAL PRIMARY KEY,
    spotify_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    number INTEGER,
    url TEXT,
    status TEXT DEFAULT 'listened' CHECK (status IN ('listened', 'active', 'upcoming')),
    is_thematic BOOLEAN DEFAULT FALSE,
    track_count INTEGER DEFAULT 0,
    invite_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id SERIAL PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    spotify_track_id TEXT NOT NULL,
    isrc TEXT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    added_by_spotify_id TEXT,
    added_at TIMESTAMPTZ,
    ai_facts TEXT,
    UNIQUE (playlist_id, spotify_track_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    id SERIAL PRIMARY KEY,
    session_track_id INTEGER REFERENCES session_tracks(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    rhymes INTEGER CHECK (rhymes BETWEEN 1 AND 5),
    structure INTEGER CHECK (structure BETWEEN 1 AND 5),
    style INTEGER CHECK (style BETWEEN 1 AND 5),
    charisma INTEGER CHECK (charisma BETWEEN 1 AND 5),
    vibe INTEGER CHECK (vibe BETWEEN 1 AND 5),
    rated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_track_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS session_participants (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS votes (
    id SERIAL PRIMARY KEY,
    session_track_id INTEGER REFERENCES session_tracks(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('keep', 'drop')),
    voted_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_track_id, telegram_id)
);
"""


MIGRATIONS = [
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS invite_url TEXT",
    """CREATE TABLE IF NOT EXISTS session_participants (
        id SERIAL PRIMARY KEY,
        session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
        telegram_id BIGINT NOT NULL,
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (session_id, telegram_id)
    )""",
]


async def apply_schema(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        for migration in MIGRATIONS:
            try:
                await conn.execute(migration)
            except Exception:
                pass
