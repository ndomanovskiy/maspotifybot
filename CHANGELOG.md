# Changelog

## 2026-11-10 (evening)
- **fix (#34):** `/scan` skips thematic playlists, as the duplicate watcher does, and reports how many it skipped. Before, it could auto-remove a deliberate repeat from a thematic playlist.
- **fix (#35):** Track titles, artists, album names, playlist names and user names are escaped before they go into Telegram HTML: `format_track`, the track card, duplicate-check messages, `/check` and the link check, `/playlist status`, `/next`, `/get`, `/stats`, `/mystats`, `/history`, the session message (playlist name and participants), `/dbinfo`, the session recap (stats and easter eggs), the secret, import, reschedule and close replies, created-playlist links and genre names. One helper, `app.utils.escape_html`, does it everywhere and shows a missing value as `?`. A name with `<` could stop such a message from being sent; for the recap that meant nobody got it. Duplicate-check message texts moved to `app/bot/duplicate_messages.py`.
- **chore (#36):** `ruff check app/` is clean: unused imports and f-strings removed, `display_name` is imported from `app.utils` everywhere, the dead GPT-4o fallback constant is gone.
- **feat:** Hand-written track facts for when the Anthropic credit balance is empty: `python -m app.services.facts pending <N>` / `save`, which refuses HTML Telegram could not show. «Перегенерировать факты» no longer wipes facts before the new ones exist.
- **docs:** CLAUDE.md, README and architecture docs no longer claim a GPT-4o fallback or a VPS deploy that do not exist.
- **data:** Kept tracks of TURDOM#108 (13) and #109 (18) were added to the genre playlists by a one-off script (59 additions); 6 tracks wait for a genre because Last.fm refuses the bot's IP.

## 2026-11-10
- **feat:** Pressing «Оставить» on a suspected duplicate or a previously dropped track is now remembered for that one playlist while it is open (nothing is stored for a closed or unknown playlist, which is never checked). The duplicate watcher skips the track there the way it skips a thematic playlist; `/scan` skips it too and reports how many it skipped. The same track added to another playlist is still checked: while its playlist is open, the kept copy no longer stops the ISRC and fuzzy steps from finding an older lookalike. The kept track is saved like any other track of the playlist, so it gets a genre and facts and goes through distribution. Kept and thematic tracks now get an ISRC lookup before saving when Spotify left it out (other tracks already had one in the duplicate check). Pressing «Удалить», or the track leaving an open playlist, forgets the decision.
- **fix:** Editing a message after «Оставить», «Удалить», approve, deny or a join request keeps its bold text and links, escapes user text (`message.html_text`) and shows no link preview. The join request from `/join` and from the old join button escapes the user's name. «Оставить» and «Удалить» take the buttons away while working; they come back only if saving fails or the track could not be removed from Spotify. Auto-removal notices label each match by its kind instead of calling every non-exact match ISRC.
- **db:** Migration 70: `kept_duplicates`.
- **chore:** Removed the VPS deploy workflow: a push to main no longer deploys anywhere.
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
