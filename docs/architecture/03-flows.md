# Main Flows

## 1. Session Lifecycle (Full)

```mermaid
sequenceDiagram
    participant Admin
    participant Bot as bot/
    participant DB
    participant Spotify
    participant Users

    Note over Admin,Users: PHASE 1: Session Creation
    Admin->>Bot: /session <playlist_url>
    Bot->>DB: Check duplicate session
    Bot->>Spotify: Get playlist info
    Bot->>DB: INSERT sessions (status=active)
    Bot->>DB: INSERT session_participants (admin)
    Bot->>Spotify: Start context + pause
    Bot->>Users: "Сессия создана! Присоединяйтесь"

    Note over Admin,Users: PHASE 2: Participants Join
    Users->>Bot: Click "Присоединиться"
    Bot->>Admin: "Пустить @user?"
    Admin->>Bot: Click "Пустить"
    Bot->>DB: INSERT session_participants
    Bot->>Users: "Ты в сессии!"

    Note over Admin,Users: PHASE 3: Listening Starts
    Admin->>Bot: Click "Запустить прослушивание"
    Bot->>Spotify: Enable shuffle + start playback
    Bot->>Bot: Start SpotifyMonitor (poll 4s)
    Bot->>Bot: Generate pre-recap teaser (background)

    Note over Admin,Users: PHASE 4: Track Playing & Voting
    loop Every track
        Bot->>Spotify: Poll playback (4s)
        Spotify-->>Bot: New track detected
        Bot->>DB: INSERT session_tracks
        Bot->>Spotify: Get added_by from playlist
        Bot->>DB: Get ai_facts from playlist_tracks
        Bot->>Users: Track card + Keep/Drop buttons

        loop Each participant votes
            Users->>Bot: Click Keep or Drop
            Bot->>DB: INSERT votes
            Bot->>DB: Count drops vs threshold
            alt Drop threshold reached (not all voted yet)
                Bot->>DB: UPDATE vote_result = 'drop' (mark only)
                Bot->>Bot: Update button counters
            else Not enough drops yet
                Bot->>Bot: Update button counters
            end
        end

        Note over Users,DB: When all participants voted
        Bot->>DB: Check vote_result
        alt vote_result = 'drop'
            Bot->>Spotify: Remove track from playlist
            Bot->>Users: Finalize card (dropped)
        else keep wins
            Bot->>DB: UPDATE vote_result = 'keep'
            Bot->>Users: Finalize card (kept)
        end
        Bot->>Spotify: Skip to next (if track is current)
    end

    Note over Admin,Users: PHASE 5: Session End
    Admin->>Bot: /end
    Bot->>Bot: Stop monitor
    Bot->>DB: UPDATE pending tracks → 'keep'
    Bot->>DB: UPDATE sessions status=ended, ended_at=NOW()
    Bot->>Users: Pre-recap teaser

    Note over Admin,Users: PHASE 6: Post-Session (auto)
    Bot->>DB: Get session stats
    Bot->>Bot: AI generate recap
    Bot->>DB: Save recap_text
    Bot->>Users: Full recap

    Bot->>DB: Get kept tracks + genres
    Bot->>Spotify: Add to genre playlists
    Bot->>DB: UPDATE distributed_at
    Bot->>Users: "Distributed X tracks"

    Bot->>Users: "Create next playlist?"
```

## 2. Vote Logic

```mermaid
sequenceDiagram
    participant User
    participant Bot as bot/
    participant Vote as voting.py
    participant DB

    User->>Bot: Click Keep/Drop button
    Bot->>Vote: record_vote(track_id, user_id, vote)
    Vote->>DB: INSERT/UPDATE votes
    Vote->>DB: COUNT drops for this track
    Vote->>DB: COUNT active participants

    Vote->>Vote: threshold = ceil(participants / 2)

    alt drop_count >= threshold (not all voted)
        Vote->>DB: UPDATE session_tracks.vote_result = 'drop'
        Vote-->>Bot: {status: voted, vote_result: drop}
        Bot->>Bot: Update button counters
    else all voted (total_votes >= participants)
        alt vote_result = 'drop'
            Vote-->>Bot: {status: finalized, vote_result: drop}
            Bot->>Bot: remove_track_from_playlist()
        else drops < threshold
            Vote->>DB: UPDATE session_tracks.vote_result = 'keep'
            Vote-->>Bot: {status: finalized, vote_result: keep}
        end
        Bot->>Bot: finalize_card()
        Bot->>Bot: skip_to_next() (if track is current)
    else waiting for more votes
        Vote-->>Bot: {status: voted}
        Bot->>Bot: Update button counters
    end
```

## 3. Duplicate Detection (Background)

```mermaid
sequenceDiagram
    participant DW as DuplicateWatcher
    participant DB
    participant Spotify
    participant GR as genre_resolver
    participant AI as ai.py
    participant User

    loop Every N seconds
        DW->>DB: Get active/upcoming playlists
        DW->>DB: Get known track IDs per playlist

        loop Each playlist
            DW->>Spotify: Get current tracks
            loop Each new track
                DW->>DB: INSERT playlist_tracks
                DW->>GR: resolve_and_save_genre(track)
                GR->>Spotify: Get artist genres
                GR->>DB: UPDATE genre

                DW->>DB: Check duplicate (track_id + ISRC)
                alt Duplicate found AND not thematic
                    DW->>Spotify: Remove track from playlist
                    DW->>DB: DELETE playlist_tracks
                    DW->>User: "Дубликат! Уже есть в [playlist]"
                end
            end
        end

        Note over DW: Also generate AI facts
        DW->>DB: Get tracks without ai_facts (upcoming playlist)
        loop Each track
            DW->>AI: generate_track_facts(title, artist)
            DW->>DB: UPDATE ai_facts
        end
    end
```

## 4. Genre Distribution

```mermaid
sequenceDiagram
    participant Admin
    participant Bot as bot/
    participant AC as admin_commands
    participant GD as genre_distributor
    participant DB
    participant Spotify

    Admin->>Bot: /distribute 91
    Bot->>AC: cmd_distribute(pool, 91)
    AC->>DB: Resolve TURDOM#91 → playlist + session
    AC->>DB: Check distributed_at

    alt Already distributed
        AC-->>Bot: "Already done. Repeat?"
        Admin->>Bot: Click "Повторить"
        Bot->>Bot: callback.answer() immediately
        Bot->>AC: cmd_distribute_force()
    end

    AC->>GD: distribute_session_tracks(session_id)

    GD->>DB: SELECT kept tracks + genres (JOIN session_tracks ↔ playlist_tracks)

    loop Each track with genre
        GD->>GD: classify_track(genre) → GENRE_MAP match
        alt Classified
            GD->>GD: Group by genre playlist
        else No match
            GD->>GD: skipped++
        end
    end

    loop Each genre playlist with tracks
        GD->>Spotify: Get all existing tracks (dedup check)
        GD->>GD: Filter out already-existing
        GD->>Spotify: playlist_add(new tracks)
    end

    GD-->>AC: {distributed: N, skipped: M}
    AC->>DB: UPDATE sessions.distributed_at
    AC->>DB: INSERT action_log
    AC-->>Bot: Result message
    Bot->>Admin: "Раскидал N треков (пропущено: M)"
```

## 5. Playlist Creation & Import

```mermaid
sequenceDiagram
    participant Admin
    participant Bot as bot/
    participant AC as admin_commands
    participant PL as playlists.py
    participant GR as genre_resolver
    participant DB
    participant Spotify

    Note over Admin,Spotify: Create Next Playlist
    Admin->>Bot: /create_next [theme]
    Bot->>AC: cmd_create_next(theme)
    AC->>DB: Check for open playlists
    alt Open playlist exists
        AC-->>Bot: "Blocked! Close existing first"
    else No open playlist
        AC->>PL: create_next_playlist(theme)
        PL->>DB: Get max(number), increment
        PL->>PL: Calculate next Wednesday
        PL->>Spotify: playlist_create(name, collaborative=true)
        PL->>DB: INSERT playlists (status=upcoming)
        PL-->>AC: {name, number, url}
        AC->>DB: INSERT action_log
        AC-->>Bot: "Created! Collaborative access automatic"
    end

    Note over Admin,Spotify: Import Playlist
    Admin->>Bot: /import <url>
    Bot->>PL: import_playlist(spotify_id)
    PL->>Spotify: Get playlist info
    PL->>DB: INSERT playlists

    loop Paginated (100 per page)
        PL->>Spotify: Get tracks
        loop Each track
            PL->>DB: INSERT playlist_tracks
            PL->>GR: resolve_and_save_genre(track)
            GR->>Spotify: Get artist → genres
            GR->>DB: UPDATE genre
        end
    end
    PL-->>Bot: "Imported: N tracks"
```

## 6. User Registration

```mermaid
sequenceDiagram
    participant User
    participant Bot as bot/
    participant DB

    User->>Bot: /reg https://open.spotify.com/user/xyz
    Bot->>Bot: Extract spotify_id from URL
    Bot->>DB: INSERT/UPDATE users (telegram_id, telegram_name, spotify_id)
    Bot->>User: "Spotify привязан: xyz"
```

## 7. Spotify Auth (Admin)

```mermaid
sequenceDiagram
    participant Admin
    participant Bot as bot/
    participant Auth as auth.py
    participant Spotify
    participant DB

    Admin->>Bot: /auth
    Bot->>Auth: start_oauth()
    Auth-->>Bot: OAuth URL
    Bot->>Admin: "Перейди по ссылке"
    Bot->>Auth: Start HTTP server (:8888)

    Admin->>Spotify: Visit URL, grant permissions
    Spotify->>Auth: Redirect with code
    Auth->>Spotify: Exchange code for token
    Auth->>Auth: Init global _spotify client
    Auth->>DB: Save refresh_token to spotify_tokens
    Auth-->>Bot: Token ready
    Bot->>Admin: "Spotify подключен!"
```

## 8. Bot Startup & Recovery

```mermaid
sequenceDiagram
    participant Main as main.py
    participant Bot as bot/
    participant Auth as auth.py
    participant DW as DuplicateWatcher
    participant DB
    participant Spotify

    Main->>DB: Create pool (asyncpg)
    Main->>DB: Apply schema + migrations
    Main->>Bot: setup_bot(pool)

    Bot->>DB: Create spotify_tokens table
    Bot->>Auth: load_token_from_db()
    Auth->>DB: Get saved refresh_token
    Auth->>Spotify: Refresh token
    Auth->>Auth: Init global _spotify client

    Bot->>DB: Check for active session
    alt Active session exists
        Bot->>DB: Load session state
        Bot->>DB: Load participants
        Bot->>Bot: Restore in-memory state
        Note over Bot: Ready to continue session
    end

    Bot->>DW: Start DuplicateWatcher (background)
    Bot->>Bot: Start Telegram polling
```
