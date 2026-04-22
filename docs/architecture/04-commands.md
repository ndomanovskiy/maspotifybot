# Bot Commands Reference

## User Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/start` | All | Help menu |
| `/start history_N` | All | Deeplink to session N details |
| `/reg <spotify_url>` | All | Register and link Spotify account |
| `/next` | Registered | Get upcoming/active playlist info |
| `/check <track_url>` | Registered | Check if track is duplicate across all playlists |
| `/stats` | Registered | Global stats: tracks, playlists, genre breakdown, per-user |
| `/mystats` | Registered | Personal stats: tracks added, vote ratio, top genres |
| `/history [N]` | Registered | Paginated session history or session N details |
| `/join` | Registered | Request to join active session |
| `/leave` | Registered | Leave current session |
| `/secret [text]` | Registered | Leave easter egg for session recap |
| `/genres` | Registered | List genre playlists with links |

## Admin Commands — Session

| Command | Description |
|---------|-------------|
| `/auth` | Start Spotify OAuth flow |
| `/session <playlist_url>` | Create session for playlist |
| `/session start` | Create session (auto-finds upcoming playlist) |
| `/session end` | End active session |
| `/session kick @user` | Kick user from session |
| `/end` | End active session |
| `/kick @username` | Remove user from session |

## Admin Commands — Post-Session

| Command | Description |
|---------|-------------|
| `/distribute <N>` | Distribute kept tracks from TURDOM#N to genre playlists |
| `/recap <N>` | Get/generate AI recap for TURDOM#N |
| `/close_playlist <N>` | Mark TURDOM#N as listened, update date |
| `/create_next [theme]` | Create next TURDOM playlist (auto-closes previous) |

## Admin Commands — Management

| Command | Description |
|---------|-------------|
| `/import <playlist_url>` | Import single playlist to DB |
| `/import_all` | Import all TURDOM playlists from Spotify account |
| `/scan` | Force duplicate scan on active/upcoming playlists |
| `/reschedule <DD/MM/YYYY>` | Change upcoming playlist date |
| `/backfill_genres` | Backfill missing genres for all tracks |
| `/preview <track>` | Preview track card rendering |
| `/dbinfo` | DB overview: sessions, playlists, users, action log |
| `/health` | Playlist health check (facts, genres) |

## Inline Buttons (Callbacks)

| Callback Data | Trigger | Action |
|---|---|---|
| `vote:keep:<track_id>` | Track card | Vote keep |
| `vote:drop:<track_id>` | Track card | Vote drop |
| `skip:<track_id>` | Track card (admin) | Skip track |
| `approve:<telegram_id>` | Join request (admin) | Approve user |
| `deny:<telegram_id>` | Join request (admin) | Deny user |
| `join_session` | Session announcement | User requests join |
| `start_listening` | Session created (admin) | Start playback |
| `confirm_end` | All tracks voted | End session |
| `continue_session` | All tracks voted | Continue listening |
| `create_playlist:normal` | Post-session | Create normal next playlist |
| `create_playlist:thematic` | Post-session | Create thematic (wait for theme input) |
| `create_playlist:skip` | Post-session | Skip playlist creation |
| `redistribute:<N>` | Already distributed | Force re-distribute |
| `redistribute:cancel` | Already distributed | Cancel |
| `rerecap:<N>` | Recap exists | Regenerate recap |
| `regen_facts:<track_id>` | Track card (admin) | Regenerate AI facts |
| `recap_page:<N>:<page>` | Recap carousel | Navigate recap pages |
| `noop` | Recap page counter | No action |
| `history:<offset>` | History pagination | Navigate pages |

## Post-Session Flow (Typical Admin Sequence)

```
/end                    → End session, auto-distribute, generate recap
/create_next            → Auto-close previous, create TURDOM#92 (upcoming)
```

Or manual (if auto-distribute failed):
```
/backfill_genres        → Fill missing genres
/distribute 91          → Distribute to genre playlists
/recap 91               → Generate/view recap
/close_playlist 91      → Close
/create_next            → Create next
```
