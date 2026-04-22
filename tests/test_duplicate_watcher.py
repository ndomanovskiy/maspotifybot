"""Tests for DuplicateWatcher — track detection, facts generation, edge cases."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from tests.conftest import (
    FakeStore, FakePool, FakePlaylistItems,
    make_track, make_item,
)
from app.services.duplicate_watcher import DuplicateWatcher


def run(coro):
    """Run async test — workaround for pytest-asyncio incompatibility with Python 3.10."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# _get_known_track_ids
# ============================================================

class TestGetKnownTrackIds:

    def test_empty_playlist_returns_empty_set(self, store, fake_pool):
        """Playlist exists but has 0 tracks — should return empty set."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        assert run(watcher._get_known_track_ids(1)) == set()

    def test_returns_all_track_ids(self, store, fake_pool):
        """Should return all spotify_track_ids for the given playlist."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        store.add_track(playlist_id=1, spotify_track_id="t1", title="A")
        store.add_track(playlist_id=1, spotify_track_id="t2", title="B")
        store.add_track(playlist_id=1, spotify_track_id="t3", title="C")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        result = run(watcher._get_known_track_ids(1))
        assert result == {"t1", "t2", "t3"}

    def test_does_not_mix_playlists(self, store, fake_pool):
        """Tracks from other playlists should not leak into the result."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        store.add_playlist(id=2, spotify_id="sp2", name="PL2")
        store.add_track(playlist_id=1, spotify_track_id="t1")
        store.add_track(playlist_id=2, spotify_track_id="t2")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        result = run(watcher._get_known_track_ids(1))
        assert result == {"t1"}
        assert "t2" not in result

    def test_many_tracks(self, store, fake_pool):
        """Verify it works with a large number of tracks (simulating growth)."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        for i in range(500):
            store.add_track(playlist_id=1, spotify_track_id=f"track_{i}")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        result = run(watcher._get_known_track_ids(1))
        assert len(result) == 500
        assert "track_0" in result
        assert "track_499" in result


# ============================================================
# _generate_missing_facts
# ============================================================

class TestGenerateMissingFacts:

    @patch("app.services.duplicate_watcher.generate_track_facts")
    def test_no_upcoming_tracks_without_facts(self, mock_gen, store, fake_pool):
        """All tracks already have facts — should not call AI."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        store.add_track(playlist_id=1, spotify_track_id="t1", title="A", ai_facts="Some facts")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())
        mock_gen.assert_not_called()

    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock)
    def test_generates_for_tracks_without_facts(self, mock_gen, mock_sp, store, fake_pool):
        """Tracks with ai_facts=NULL should get facts generated."""
        mock_gen.return_value = "Cool fact about the track"

        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        t1 = store.add_track(playlist_id=1, spotify_track_id="t1", title="Track A", artist="Artist A")
        t2 = store.add_track(playlist_id=1, spotify_track_id="t2", title="Track B", artist="Artist B",
                             ai_facts="Already has facts")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())

        mock_gen.assert_called_once()
        args = mock_gen.call_args
        assert args[0][0] == "Track A"
        assert args[0][1] == "Artist A"
        assert t1["ai_facts"] == "Cool fact about the track"
        assert t2["ai_facts"] == "Already has facts"

    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock)
    def test_skips_listened_playlists(self, mock_gen, store, fake_pool):
        """Should only generate facts for upcoming playlists, not listened."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="listened")
        store.add_track(playlist_id=1, spotify_track_id="t1", title="Old Track")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())
        mock_gen.assert_not_called()

    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock)
    def test_ai_returns_empty_string(self, mock_gen, mock_sp, store, fake_pool):
        """If AI returns empty string, ai_facts should remain NULL."""
        mock_gen.return_value = ""

        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        t = store.add_track(playlist_id=1, spotify_track_id="t1", title="X")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())

        mock_gen.assert_called_once()
        assert t["ai_facts"] is None

    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock)
    def test_ai_failure_does_not_stop_other_tracks(self, mock_gen, mock_sp, store, fake_pool):
        """If AI fails on one track, should continue with the rest."""
        mock_gen.side_effect = [Exception("API down"), "Facts for B"]

        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        t1 = store.add_track(playlist_id=1, spotify_track_id="t1", title="A")
        t2 = store.add_track(playlist_id=1, spotify_track_id="t2", title="B")

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())

        assert t1["ai_facts"] is None
        assert t2["ai_facts"] == "Facts for B"

    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock)
    def test_generates_for_all_tracks_when_all_null(self, mock_gen, mock_sp, store, fake_pool):
        """When all tracks lack facts (like after bulk import), all should be generated."""
        mock_gen.return_value = "A fact"

        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        tracks = []
        for i in range(28):
            tracks.append(store.add_track(
                playlist_id=1, spotify_track_id=f"t{i}", title=f"Track {i}", artist=f"Artist {i}"
            ))

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._generate_missing_facts())

        assert mock_gen.call_count == 28
        for t in tracks:
            assert t["ai_facts"] == "A fact"


# ============================================================
# _check_playlists — new track detection
# ============================================================

class TestCheckPlaylistsNewTracks:

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_new_track_saved_to_db(self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool):
        """A new track in Spotify should be inserted into playlist_tracks."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")

        track = make_track("new_id", "New Song", "New Artist")
        item = make_item(track, added_by="user1")

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())

        saved = store.get_track(1, "new_id")
        assert saved is not None
        assert saved["title"] == "New Song"
        assert saved["artist"] == "New Artist"

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_existing_track_not_duplicated(self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool):
        """Track already in DB should be skipped — no duplicate insert."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
        store.add_track(playlist_id=1, spotify_track_id="existing_id", title="Old")

        track = make_track("existing_id", "Old")
        item = make_item(track)

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())

        count = sum(1 for t in store.playlist_tracks if t["spotify_track_id"] == "existing_id")
        assert count == 1

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_empty_playlist_no_crash(self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool):
        """Playlist with 0 tracks in Spotify should not crash."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[])
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())
        assert len(store.playlist_tracks) == 0

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_null_track_in_item_skipped(self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool):
        """Items with track=None should be skipped gracefully."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(None)])
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())
        assert len(store.playlist_tracks) == 0

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_listened_playlist_ignored(self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool):
        """Playlists with status 'listened' should not be checked."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="listened")

        sp = AsyncMock()
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())
        sp.playlist_items.assert_not_called()

    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_spotify_api_failure_continues_to_next_playlist(
        self, mock_get_sp, mock_isrc, mock_dup, store, fake_pool
    ):
        """If Spotify API fails for one playlist, should continue with others."""
        store.add_playlist(id=1, spotify_id="sp_fail", name="Failing", status="upcoming")
        store.add_playlist(id=2, spotify_id="sp_ok", name="Working", status="upcoming")

        track = make_track("t1", "Song")
        item = make_item(track)

        sp = AsyncMock()
        sp.playlist_items.side_effect = [Exception("Spotify down"), FakePlaylistItems(items=[item])]
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())
        run(watcher._check_playlists())
        assert store.get_track(2, "t1") is not None


# ============================================================
# _check_playlists — duplicate detection & removal
# ============================================================

async def _classify_as_kept(pool, duplicates):
    """Test helper: classify all duplicates as 'keep' (real duplicates)."""
    for d in duplicates:
        d["session_status"] = "keep"
    return duplicates


async def _classify_as_phantom(pool, duplicates):
    """Test helper: classify all duplicates as 'phantom'."""
    for d in duplicates:
        d["session_status"] = "phantom"
    return duplicates


async def _classify_as_dropped(pool, duplicates):
    """Test helper: classify all duplicates as 'drop'."""
    for d in duplicates:
        d["session_status"] = "drop"
    return duplicates


class TestCheckPlaylistsDuplicates:

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_kept)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_duplicate_removed_from_spotify_and_db(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """When a duplicate is found, track should be removed from Spotify and DB."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)

        track = make_track("dup_track", "Dup Song")
        item = make_item(track)

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        sp.playlist_remove.assert_called_once()
        assert store.get_track(1, "dup_track") is None
        notify.assert_called_once()

    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_thematic_playlist_allows_duplicates(self, mock_get_sp, mock_dup, mock_isrc, store, fake_pool):
        """Thematic playlists should keep duplicates — no removal, no notification."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1 - Theme", status="upcoming", is_thematic=True)

        track = make_track("dup_track", "Dup Song")
        item = make_item(track)

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        sp.playlist_remove.assert_not_called()
        notify.assert_not_called()
        assert store.get_track(1, "dup_track") is not None

    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_self_match_filtered_out(self, mock_get_sp, mock_dup, mock_isrc, store, fake_pool):
        """Duplicate in the SAME playlist should be filtered out — not treated as duplicate."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)

        track = make_track("t1", "Song")
        item = make_item(track)

        mock_dup.return_value = [
            {"match": "exact", "title": "Song", "artist": "A", "playlist": "PL1", "playlist_id": 1, "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        sp.playlist_remove.assert_not_called()
        notify.assert_not_called()

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_kept)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_notify_includes_added_by_name(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """Notification should include added_by_name when user is known."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)
        store.add_user(telegram_id=123, spotify_id="sp_user", telegram_name="Nikita", telegram_username="ndomanovskiy")

        track = make_track("dup_track", "Dup Song")
        item = make_item(track, added_by="sp_user")

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        notify.assert_called_once()
        call_kwargs = notify.call_args
        assert call_kwargs.kwargs.get("added_by_name") == "@ndomanovskiy"

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_kept)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_notify_without_added_by(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """When added_by is unknown, added_by_name should be None."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)

        track = make_track("dup_track", "Dup Song")
        item = make_item(track, added_by=None)

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        notify.assert_called_once()
        call_kwargs = notify.call_args
        assert call_kwargs.kwargs.get("added_by_name") is None

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_kept)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_added_by_name_falls_back_to_telegram_name(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """When telegram_username is empty, should use telegram_name."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)
        store.add_user(telegram_id=123, spotify_id="sp_user", telegram_name="Nikita", telegram_username="")

        track = make_track("dup_track", "Dup Song")
        item = make_item(track, added_by="sp_user")

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        notify.assert_called_once()
        call_kwargs = notify.call_args
        assert call_kwargs.kwargs.get("added_by_name") == "Nikita"

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_kept)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_fuzzy_confirm_includes_added_by_name(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """Fuzzy duplicate confirmation should also include added_by_name."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)
        store.add_user(telegram_id=123, spotify_id="sp_user", telegram_name="Nikita", telegram_username="ndomanovskiy")

        track = make_track("dup_track", "Dup Song")
        item = make_item(track, added_by="sp_user")

        mock_dup.return_value = [
            {"match": "fuzzy_exact", "title": "Dup Song!", "artist": "A", "playlist": "PL_OLD", "playlist_id": 2, "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        confirm = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify, confirm)
        run(watcher._check_playlists())

        confirm.assert_called_once()
        call_kwargs = confirm.call_args
        assert call_kwargs.kwargs.get("added_by_name") == "@ndomanovskiy"

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_phantom)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_phantom_duplicate_skipped(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """Phantom duplicates (in DB but never played in session) should be skipped."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)

        track = make_track("dup_track", "Dup Song")
        item = make_item(track)

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "playlist_id": 2, "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify)
        run(watcher._check_playlists())

        sp.playlist_remove.assert_not_called()
        notify.assert_not_called()
        assert store.get_track(1, "dup_track") is not None

    @patch("app.services.duplicate_watcher.classify_duplicates", new_callable=AsyncMock, side_effect=_classify_as_dropped)
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_dropped_duplicate_asks_confirmation(self, mock_get_sp, mock_dup, mock_isrc, mock_classify, store, fake_pool):
        """Dropped duplicates should ask for confirmation, not auto-remove."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=False)

        track = make_track("dup_track", "Dup Song")
        item = make_item(track)

        mock_dup.return_value = [
            {"match": "exact", "title": "Dup Song", "artist": "A", "playlist": "PL_OLD", "playlist_id": 2, "url": "..."}
        ]

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        mock_get_sp.return_value = sp

        notify = AsyncMock()
        confirm = AsyncMock()
        watcher = DuplicateWatcher(fake_pool, notify, confirm)
        run(watcher._check_playlists())

        sp.playlist_remove.assert_not_called()
        notify.assert_not_called()
        confirm.assert_called_once()
        # Duplicate not saved to DB (check-before-save)
        assert store.get_track(1, "dup_track") is None


# ============================================================
# _get_interval
# ============================================================

class TestGetInterval:

    def _make_watcher(self):
        return DuplicateWatcher(MagicMock(), AsyncMock())

    @patch("app.services.duplicate_watcher.datetime")
    def test_wednesday_morning_utc(self, mock_dt):
        """Wednesday 10:00 UTC = 13:00 MSK (before 14:00) -> 1 hour."""
        mock_dt.now.return_value = datetime(2026, 4, 1, 10, 0)
        watcher = self._make_watcher()
        assert watcher._get_interval() == 3600

    @patch("app.services.duplicate_watcher.datetime")
    def test_wednesday_afternoon_utc(self, mock_dt):
        """Wednesday 14:00 UTC = 17:00 MSK (after 14:00) -> 24 hours."""
        mock_dt.now.return_value = datetime(2026, 4, 1, 14, 0)
        watcher = self._make_watcher()
        assert watcher._get_interval() == 86400

    @patch("app.services.duplicate_watcher.datetime")
    def test_monday(self, mock_dt):
        """Monday -> ~5x per day."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 12, 0)
        watcher = self._make_watcher()
        assert watcher._get_interval() == 17280

    @patch("app.services.duplicate_watcher.datetime")
    def test_tuesday(self, mock_dt):
        """Tuesday -> every hour."""
        mock_dt.now.return_value = datetime(2026, 3, 31, 12, 0)
        watcher = self._make_watcher()
        assert watcher._get_interval() == 3600

    @patch("app.services.duplicate_watcher.datetime")
    def test_weekend(self, mock_dt):
        """Saturday -> 2x per day."""
        mock_dt.now.return_value = datetime(2026, 4, 4, 12, 0)
        watcher = self._make_watcher()
        assert watcher._get_interval() == 43200


# ============================================================
# Integration: _check_playlists + _generate_missing_facts together
# ============================================================

class TestIntegration:

    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock, return_value="AI fact")
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_new_track_gets_facts_on_next_generate_pass(
        self, mock_get_sp, mock_isrc, mock_dup, mock_gen, store, fake_pool
    ):
        """Full flow: new track added -> _check_playlists saves it -> _generate_missing_facts generates facts."""
        store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")

        track = make_track("t1", "New Song", "Cool Artist")
        item = make_item(track)

        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=[item])
        sp.track.return_value = track
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())

        # Step 1: check_playlists saves track to DB without facts
        run(watcher._check_playlists())
        saved = store.get_track(1, "t1")
        assert saved is not None
        assert saved["ai_facts"] is None

        # Step 2: generate_missing_facts fills in the facts
        run(watcher._generate_missing_facts())
        assert saved["ai_facts"] == "AI fact"

    @patch("app.services.duplicate_watcher.generate_track_facts", new_callable=AsyncMock, return_value="AI fact")
    @patch("app.services.duplicate_watcher.check_duplicate", new_callable=AsyncMock, return_value=[])
    @patch("app.services.duplicate_watcher.get_track_isrc", new_callable=AsyncMock, return_value=None)
    @patch("app.services.duplicate_watcher.get_spotify", new_callable=AsyncMock)
    def test_bulk_imported_tracks_get_facts(
        self, mock_get_sp, mock_isrc, mock_dup, mock_gen, store, fake_pool
    ):
        """Reproduces the original bug: tracks imported without facts should get facts via _generate_missing_facts."""
        store.add_playlist(id=1, spotify_id="sp1", name="TURDOM#91", status="upcoming")

        # Simulate bulk import — 28 tracks in DB, all without facts
        for i in range(28):
            store.add_track(
                playlist_id=1, spotify_track_id=f"t{i}",
                title=f"Track {i}", artist=f"Artist {i}",
                ai_facts=None,
            )

        # Spotify returns same 28 tracks
        items = [make_item(make_track(f"t{i}", f"Track {i}", f"Artist {i}")) for i in range(28)]
        sp = AsyncMock()
        sp.playlist_items.return_value = FakePlaylistItems(items=items)
        sp.track.return_value = make_track("t0", "Track 0", "Artist 0")  # mock for release_date lookup
        mock_get_sp.return_value = sp

        watcher = DuplicateWatcher(fake_pool, AsyncMock())

        # check_playlists should skip all (already in DB)
        run(watcher._check_playlists())

        # generate_missing_facts should generate for all 28
        run(watcher._generate_missing_facts())

        assert mock_gen.call_count == 28
        for i in range(28):
            t = store.get_track(1, f"t{i}")
            assert t["ai_facts"] == "AI fact", f"Track t{i} missing facts"
