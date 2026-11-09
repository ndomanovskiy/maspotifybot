# Changelog

## 2026-11-10
- **fix:** ISRC was never read: tekore returns `external_ids` as a dict, while the code used `getattr(external_ids, "isrc", None)`, so every track was stored without it and the ISRC duplicate step never fired. Added `extract_isrc`, used in all 4 places.
- **data:** Filled `tracks.isrc` for 2962 of 2964 tracks from Spotify (one-off script, no duplicates removed). Same-recording pairs across history: 6, latest in TURDOM#93.
- **feat:** New playlist no longer notifies anyone automatically. The admin gets a private picker with a tick per registered user and a Send button (`app/bot/playlist_notify.py`).
- **fix:** ISRC match ignores modified versions (remix, sped up), which stay sibling warnings. Watcher upsert now fills a missing ISRC on known tracks. Playlist names are escaped in "created" messages.
- **db:** Migration 69: index on `tracks.isrc`.
- **ops:** Spotify refresh tokens now expire after 6 months: re-run `/auth` by about 2027-05-08.

## 2026-05-20
- **fix:** `session_tracks.added_by_spotify_id` was NULL for some tracks even when `playlist_tracks` had the attribution. Root cause: `SpotifyMonitor._get_added_by` used `playlist_items(limit=50)` without pagination, so tracks past the first page were missed (TURDOM playlists routinely exceed 50). Session 13 (TURDOM#98) lost author on 2/32 tracks.
- **fix:** Add pagination (`limit=100` + `offset`) to `_get_added_by` with defensive `total` handling.
- **fix:** `create_session_track` now falls back to `playlist_tracks` when monitor returns `None` or empty string, and returns `(id, resolved_added_by)` so the track card in Telegram shows 👤 author even after a monitor miss.
- **db:** Migration 68 — backfill existing NULL `session_tracks.added_by_spotify_id` from `playlist_tracks` (idempotent).
- **test:** 5 new cases in `TestCreateSessionTrackFallback` covering fallback used / not used / both-null / `""`-normalized to `None`.

## 2026-04-29
- **fix:** duplicate detection: `classify_duplicates()` marked all tracks without `session_tracks` entries as `phantom`, which got filtered out by `on_spotify_link`. Since sessions only started at TURDOM#91, all tracks from playlists #1-#90 (status `listened`, ~2400 tracks) were invisible to duplicate detection. Fix: check playlist status — `listened` playlists get `keep` (played before session system), `active`/`upcoming` keep `phantom`. Added `pl_status_cache` to avoid repeated DB queries per playlist.
- **test:** 7 new tests for `classify_duplicates` phantom/keep logic (`tests/test_classify_duplicates.py`)
