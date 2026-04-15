import asyncpg
import logging

log = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);
"""


# Numbered migrations — each runs exactly once, tracked by schema_version.
# NEVER reorder or edit existing entries. Only append new ones.
VERSIONED_MIGRATIONS: list[tuple[int, str, str]] = [
    # --- legacy (already applied via old MIGRATIONS list) ---
    (1, "invite_url column",
     "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS invite_url TEXT"),
    (2, "telegram_username column",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_username TEXT"),
    (3, "session_participants active flag",
     "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE"),
    (4, "session_participants left_at",
     "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ"),
    (5, "recap_text column",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS recap_text TEXT"),
    (6, "distributed_at column",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS distributed_at TIMESTAMPTZ"),
    (7, "genre column",
     "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS genre TEXT"),
    (8, "secret_note column",
     "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS secret_note TEXT"),
    (9, "current_track_id for session recovery",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS current_track_id INTEGER"),
    (10, "track_messages table", """
        CREATE TABLE IF NOT EXISTS track_messages (
            id SERIAL PRIMARY KEY,
            session_track_id INTEGER REFERENCES session_tracks(id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            message_id INTEGER NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            UNIQUE (session_track_id, chat_id)
        )"""),
    (11, "action_log table", """
        CREATE TABLE IF NOT EXISTS action_log (
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
        )"""),

    # --- Phase 4: indexes ---
    (12, "idx: session_tracks.session_id",
     "CREATE INDEX IF NOT EXISTS idx_session_tracks_session_id ON session_tracks (session_id)"),
    (13, "idx: session_tracks.spotify_track_id",
     "CREATE INDEX IF NOT EXISTS idx_session_tracks_spotify_track_id ON session_tracks (spotify_track_id)"),
    (14, "idx: session_tracks.added_by_spotify_id",
     "CREATE INDEX IF NOT EXISTS idx_session_tracks_added_by ON session_tracks (added_by_spotify_id)"),
    (15, "idx: session_participants.session_id",
     "CREATE INDEX IF NOT EXISTS idx_session_participants_session_id ON session_participants (session_id)"),
    (16, "idx: session_participants.telegram_id",
     "CREATE INDEX IF NOT EXISTS idx_session_participants_telegram_id ON session_participants (telegram_id)"),
    (17, "idx: votes.session_track_id",
     "CREATE INDEX IF NOT EXISTS idx_votes_session_track_id ON votes (session_track_id)"),
    (18, "idx: votes.telegram_id",
     "CREATE INDEX IF NOT EXISTS idx_votes_telegram_id ON votes (telegram_id)"),
    (19, "idx: playlist_tracks.spotify_track_id",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_spotify_track_id ON playlist_tracks (spotify_track_id)"),
    (20, "idx: playlist_tracks.isrc",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_isrc ON playlist_tracks (isrc)"),
    (21, "idx: playlist_tracks.added_by_spotify_id",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_added_by ON playlist_tracks (added_by_spotify_id)"),
    (22, "idx: playlists.number",
     "CREATE INDEX IF NOT EXISTS idx_playlists_number ON playlists (number)"),
    (23, "idx: playlists.status",
     "CREATE INDEX IF NOT EXISTS idx_playlists_status ON playlists (status)"),
    (24, "idx: sessions.status",
     "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status)"),
    (25, "idx: sessions.playlist_spotify_id",
     "CREATE INDEX IF NOT EXISTS idx_sessions_playlist_spotify_id ON sessions (playlist_spotify_id)"),
    (26, "idx: users.spotify_id",
     "CREATE INDEX IF NOT EXISTS idx_users_spotify_id ON users (spotify_id)"),
    (27, "idx: users.telegram_username",
     "CREATE INDEX IF NOT EXISTS idx_users_telegram_username ON users (telegram_username)"),
    (28, "idx: ratings.telegram_id",
     "CREATE INDEX IF NOT EXISTS idx_ratings_telegram_id ON ratings (telegram_id)"),
    (29, "idx: track_messages.session_track_id",
     "CREATE INDEX IF NOT EXISTS idx_track_messages_session_track_id ON track_messages (session_track_id)"),

    # --- Phase 4: foreign keys on telegram_id ---
    (30, "fk: session_participants.telegram_id -> users",
     "ALTER TABLE session_participants ADD CONSTRAINT fk_sp_telegram_id FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE"),
    (31, "fk: votes.telegram_id -> users",
     "ALTER TABLE votes ADD CONSTRAINT fk_votes_telegram_id FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE"),
    (32, "fk: ratings.telegram_id -> users",
     "ALTER TABLE ratings ADD CONSTRAINT fk_ratings_telegram_id FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE"),

    # --- Fuzzy duplicate detection ---
    (33, "normalized_title column",
     "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS normalized_title TEXT"),
    (34, "normalized_artist column",
     "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS normalized_artist TEXT"),
    (35, "idx: playlist_tracks.normalized_artist",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_normalized_artist ON playlist_tracks (normalized_artist)"),
    (36, "idx: playlist_tracks.normalized_title",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_normalized_title ON playlist_tracks (normalized_title)"),

    # --- Schema v2: tracks table (normalize data model) ---
    (37, "tracks table", """
        CREATE TABLE IF NOT EXISTS tracks (
            id SERIAL PRIMARY KEY,
            spotify_track_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT,
            cover_url TEXT,
            isrc TEXT,
            genre TEXT,
            ai_facts TEXT,
            normalized_title TEXT,
            normalized_artist TEXT,
            genre_approved BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )"""),
    (38, "populate tracks from playlist_tracks", """
        INSERT INTO tracks (spotify_track_id, title, artist, isrc, genre, ai_facts, normalized_title, normalized_artist)
        SELECT DISTINCT ON (spotify_track_id)
            spotify_track_id, title, artist, isrc, genre, ai_facts, normalized_title, normalized_artist
        FROM playlist_tracks
        ORDER BY spotify_track_id, id
        ON CONFLICT (spotify_track_id) DO NOTHING"""),
    (39, "populate tracks from session_tracks (album, cover_url)", """
        UPDATE tracks t SET
            album = st.album,
            cover_url = st.cover_url
        FROM session_tracks st
        WHERE t.spotify_track_id = st.spotify_track_id
        AND (t.album IS NULL OR t.cover_url IS NULL)"""),
    (40, "playlist_tracks: add track_id FK column",
     "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS track_id INTEGER"),
    (41, "playlist_tracks: populate track_id", """
        UPDATE playlist_tracks pt SET track_id = t.id
        FROM tracks t
        WHERE pt.spotify_track_id = t.spotify_track_id AND pt.track_id IS NULL"""),
    (42, "playlist_tracks: add status column",
     "ALTER TABLE playlist_tracks ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'"),
    (43, "session_tracks: add track_id FK column",
     "ALTER TABLE session_tracks ADD COLUMN IF NOT EXISTS track_id INTEGER"),
    (44, "session_tracks: populate track_id", """
        UPDATE session_tracks st SET track_id = t.id
        FROM tracks t
        WHERE st.spotify_track_id = t.spotify_track_id AND st.track_id IS NULL"""),
    (45, "idx: tracks.spotify_track_id",
     "CREATE INDEX IF NOT EXISTS idx_tracks_spotify_track_id ON tracks (spotify_track_id)"),
    (46, "idx: tracks.normalized_artist",
     "CREATE INDEX IF NOT EXISTS idx_tracks_normalized_artist ON tracks (normalized_artist)"),
    (47, "idx: tracks.normalized_title",
     "CREATE INDEX IF NOT EXISTS idx_tracks_normalized_title ON tracks (normalized_title)"),
    (48, "idx: tracks.genre_approved",
     "CREATE INDEX IF NOT EXISTS idx_tracks_genre_approved ON tracks (genre_approved)"),
    (49, "idx: playlist_tracks.track_id",
     "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track_id ON playlist_tracks (track_id)"),
    (50, "idx: session_tracks.track_id",
     "CREATE INDEX IF NOT EXISTS idx_session_tracks_track_id ON session_tracks (track_id)"),

    # --- sessions: normalize playlist reference ---
    (51, "sessions: add playlist_id FK column",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS playlist_id INTEGER"),
    (52, "sessions: populate playlist_id from playlist_spotify_id", """
        UPDATE sessions s SET playlist_id = p.id
        FROM playlists p
        WHERE s.playlist_spotify_id = p.spotify_id AND s.playlist_id IS NULL"""),
    (53, "idx: sessions.playlist_id",
     "CREATE INDEX IF NOT EXISTS idx_sessions_playlist_id ON sessions (playlist_id)"),

    # --- Normalize user references ---
    (54, "session_participants: add user_id FK column",
     "ALTER TABLE session_participants ADD COLUMN IF NOT EXISTS user_id INTEGER"),
    (55, "session_participants: populate user_id from telegram_id", """
        UPDATE session_participants sp SET user_id = u.id
        FROM users u
        WHERE sp.telegram_id = u.telegram_id AND sp.user_id IS NULL"""),
    (56, "votes: add participant_id FK column",
     "ALTER TABLE votes ADD COLUMN IF NOT EXISTS participant_id INTEGER"),
    (57, "votes: populate participant_id", """
        UPDATE votes v SET participant_id = sp.id
        FROM session_participants sp
        JOIN session_tracks st ON st.session_id = sp.session_id
        WHERE v.session_track_id = st.id
        AND v.telegram_id = sp.telegram_id
        AND v.participant_id IS NULL"""),
    (58, "idx: session_participants.user_id",
     "CREATE INDEX IF NOT EXISTS idx_session_participants_user_id ON session_participants (user_id)"),
    (59, "idx: votes.participant_id",
     "CREATE INDEX IF NOT EXISTS idx_votes_participant_id ON votes (participant_id)"),

    # --- Fix: drop NOT NULL on denormalized columns (data now in tracks table) ---
    (60, "playlist_tracks: title nullable",
     "ALTER TABLE playlist_tracks ALTER COLUMN title DROP NOT NULL"),
    (61, "playlist_tracks: artist nullable",
     "ALTER TABLE playlist_tracks ALTER COLUMN artist DROP NOT NULL"),
    (62, "session_tracks: title nullable",
     "ALTER TABLE session_tracks ALTER COLUMN title DROP NOT NULL"),
    (63, "session_tracks: artist nullable",
     "ALTER TABLE session_tracks ALTER COLUMN artist DROP NOT NULL"),
]

# Note: backfill of normalized_title/normalized_artist for existing tracks
# is done via Python (not SQL) because normalization logic lives in app code.
# Run /backfill_normalized in bot after deploy.


async def apply_schema(pool: asyncpg.Pool):
    """Apply base schema + run versioned migrations."""
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)

        # Bootstrap: mark legacy migrations as applied if schema_version is fresh
        # but tables already exist (i.e. upgrading from old migration system)
        existing_versions = set()
        try:
            rows = await conn.fetch("SELECT version FROM schema_version")
            existing_versions = {r["version"] for r in rows}
        except Exception:
            pass

        for version, description, sql in VERSIONED_MIGRATIONS:
            if version in existing_versions:
                continue
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    version, description,
                )
                log.info(f"Migration v{version}: {description}")
            except Exception as e:
                # FK/index may fail on existing data — log and continue
                log.warning(f"Migration v{version} ({description}) skipped: {e}")
                await conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    version, f"SKIPPED: {description} — {e}",
                )
