# Changelog

## 2026-05-20
- **fix:** `session_tracks.added_by_spotify_id` was NULL for some tracks even when `playlist_tracks` had the attribution. Root cause: `SpotifyMonitor._get_added_by` used `playlist_items(limit=50)` without pagination, so tracks past the first page were missed (TURDOM playlists routinely exceed 50). Session 13 (TURDOM#98) lost author on 2/32 tracks.
- **fix:** Add pagination (`limit=100` + `offset`) to `_get_added_by` with defensive `total` handling.
- **fix:** `create_session_track` now falls back to `playlist_tracks` when monitor returns `None` or empty string, and returns `(id, resolved_added_by)` so the track card in Telegram shows 👤 author even after a monitor miss.
- **db:** Migration 68 — backfill existing NULL `session_tracks.added_by_spotify_id` from `playlist_tracks` (idempotent).
- **test:** 5 new cases in `TestCreateSessionTrackFallback` covering fallback used / not used / both-null / `""`-normalized to `None`.

## 2026-04-29
- **fix:** duplicate detection: `classify_duplicates()` marked all tracks without `session_tracks` entries as `phantom`, which got filtered out by `on_spotify_link`. Since sessions only started at TURDOM#91, all tracks from playlists #1-#90 (status `listened`, ~2400 tracks) were invisible to duplicate detection. Fix: check playlist status — `listened` playlists get `keep` (played before session system), `active`/`upcoming` keep `phantom`. Added `pl_status_cache` to avoid repeated DB queries per playlist.
- **test:** 7 new tests for `classify_duplicates` phantom/keep logic (`tests/test_classify_duplicates.py`)
