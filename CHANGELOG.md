# Changelog

## 2026-04-29
- **fix:** duplicate detection: `classify_duplicates()` marked all tracks without `session_tracks` entries as `phantom`, which got filtered out by `on_spotify_link`. Since sessions only started at TURDOM#91, all tracks from playlists #1-#90 (status `listened`, ~2400 tracks) were invisible to duplicate detection. Fix: check playlist status — `listened` playlists get `keep` (played before session system), `active`/`upcoming` keep `phantom`. Added `pl_status_cache` to avoid repeated DB queries per playlist.
- **test:** 7 new tests for `classify_duplicates` phantom/keep logic (`tests/test_classify_duplicates.py`)
