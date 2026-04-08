import asyncpg

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    telegram_name TEXT NOT NULL,
    telegram_username TEXT,
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
    left_at TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_username TEXT",
    "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
    "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ",
    """CREATE TABLE IF NOT EXISTS session_participants (
        id SERIAL PRIMARY KEY,
        session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
        telegram_id BIGINT NOT NULL,
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (session_id, telegram_id)
    )""",
    # v2: admin commands & action logging
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS recap_text TEXT",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS distributed_at TIMESTAMPTZ",
    "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS genre TEXT",
    # v3: easter eggs
    "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS secret_note TEXT",
    """CREATE TABLE IF NOT EXISTS action_log (
        id SERIAL PRIMARY KEY,
        action TEXT NOT NULL,
        turdom_number INTEGER,
        session_id INTEGER,
        playlist_id INTEGER,
        triggered_by BIGINT,
        params JSONB,
        result JSONB,
        status TEXT DEFAULT 'ok',
        created_at TIMESTAMPTZ DEFAULT NOW()
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
