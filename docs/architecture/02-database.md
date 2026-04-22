# Database Schema

## ER Diagram

```mermaid
erDiagram
    users {
        serial id PK
        bigint telegram_id UK
        text telegram_name
        text telegram_username
        text spotify_id
        boolean is_admin
        timestamptz created_at
    }

    playlists {
        serial id PK
        text spotify_id UK
        text name
        integer number
        text url
        text status "listened | active | upcoming"
        boolean is_thematic
        integer track_count
        text invite_url "(deprecated, unused)"
        timestamptz created_at
    }

    playlist_tracks {
        serial id PK
        integer playlist_id FK
        text spotify_track_id
        text isrc
        text title
        text artist
        text added_by_spotify_id
        timestamptz added_at
        text ai_facts
        text genre
    }

    sessions {
        serial id PK
        text playlist_spotify_id
        text playlist_name
        integer current_track_id
        timestamptz started_at
        timestamptz ended_at
        text status "active | ended"
        text recap_text
        timestamptz distributed_at
    }

    session_tracks {
        serial id PK
        integer session_id FK
        text spotify_track_id
        text title
        text artist
        text album
        text cover_url
        text added_by_spotify_id
        integer position
        text vote_result "pending | keep | drop"
        timestamptz created_at
    }

    session_participants {
        serial id PK
        integer session_id FK
        bigint telegram_id FK
        text secret_note
        timestamptz joined_at
        timestamptz left_at
        boolean active
    }

    votes {
        serial id PK
        integer session_track_id FK
        bigint telegram_id FK
        text vote "keep | drop"
        timestamptz voted_at
    }

    ratings {
        serial id PK
        integer session_track_id FK
        bigint telegram_id FK
        integer rhymes
        integer structure
        integer style
        integer charisma
        integer vibe
        timestamptz rated_at
    }

    action_log {
        serial id PK
        text action
        integer turdom_number
        integer session_id
        integer playlist_id
        bigint triggered_by
        jsonb params
        jsonb result
        text status
        timestamptz created_at
    }

    spotify_tokens {
        serial id PK
        text refresh_token
        text access_token
        timestamptz expires_at
    }

    schema_version {
        integer version PK
        timestamptz applied_at
        text description
    }

    track_messages {
        serial id PK
        integer session_track_id FK
        bigint chat_id
        integer message_id
        text caption
    }

    playlists ||--o{ playlist_tracks : has
    sessions ||--o{ session_tracks : has
    sessions ||--o{ session_participants : has
    session_tracks ||--o{ votes : has
    session_tracks ||--o{ ratings : has
    session_tracks ||--o{ track_messages : has
    users ||--o{ session_participants : has
    users ||--o{ votes : has
    users ||--o{ ratings : has
```

## Key Relationships

| Relationship | Type | Join Key |
|---|---|---|
| playlists → playlist_tracks | 1:N | playlist_tracks.playlist_id |
| sessions → session_tracks | 1:N | session_tracks.session_id |
| sessions → session_participants | 1:N | session_participants.session_id |
| session_tracks → votes | 1:N | votes.session_track_id |
| session_tracks → ratings | 1:N | ratings.session_track_id |
| playlists → sessions | 1:N | sessions.playlist_spotify_id = playlists.spotify_id |
| session_tracks → track_messages | 1:N | track_messages.session_track_id |
| users → session_participants | 1:N FK | session_participants.telegram_id → users.telegram_id (ON DELETE CASCADE) |
| users → votes | 1:N FK | votes.telegram_id → users.telegram_id (ON DELETE CASCADE) |
| users → ratings | 1:N FK | ratings.telegram_id → users.telegram_id (ON DELETE CASCADE) |
| session_tracks ↔ playlist_tracks | Implicit | spotify_track_id (no FK) |
| users ↔ playlist_tracks | Implicit | added_by_spotify_id = users.spotify_id (no FK) |

## Notes

- **3 foreign keys on telegram_id** — `session_participants`, `votes`, `ratings` reference `users.telegram_id` (ON DELETE CASCADE)
- **18 indexes** on frequently queried columns (Phase 4)
- **Versioned migrations** tracked in `schema_version` (32 migrations)
- `playlist_tracks` unique constraint: `(playlist_id, spotify_track_id)` — same track can exist in multiple playlists
- `votes` unique constraint: `(session_track_id, telegram_id)` — one vote per user per track
- `session_tracks` ↔ `playlist_tracks` joined via `spotify_track_id` — used in distribute to get genre
- `genre` column added via migration v2, backfilled via `genre_resolver.py`
