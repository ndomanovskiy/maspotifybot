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
        text invite_url
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
        bigint telegram_id
        timestamptz joined_at
        timestamptz left_at
        boolean active
    }

    votes {
        serial id PK
        integer session_track_id FK
        bigint telegram_id
        text vote "keep | drop"
        timestamptz voted_at
    }

    ratings {
        serial id PK
        integer session_track_id FK
        bigint telegram_id
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

    playlists ||--o{ playlist_tracks : has
    sessions ||--o{ session_tracks : has
    sessions ||--o{ session_participants : has
    session_tracks ||--o{ votes : has
    session_tracks ||--o{ ratings : has
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
| session_tracks ↔ playlist_tracks | Implicit | spotify_track_id (no FK) |
| users ↔ session_participants | Implicit | telegram_id (no FK) |
| users ↔ playlist_tracks | Implicit | added_by_spotify_id = users.spotify_id (no FK) |

## Notes

- **No foreign keys enforced** — all relationships are implicit via matching columns
- `playlist_tracks` unique constraint: `(playlist_id, spotify_track_id)` — same track can exist in multiple playlists
- `votes` unique constraint: `(session_track_id, telegram_id)` — one vote per user per track
- `session_tracks` ↔ `playlist_tracks` joined via `spotify_track_id` — used in distribute to get genre
- `genre` column added via migration v2, backfilled via `genre_resolver.py`
