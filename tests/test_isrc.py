"""ISRC handling: what the bot reads must match what tekore really returns."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from tekore.model import FullTrack

from app.services.duplicate_watcher import DuplicateWatcher
from app.services.playlists import check_duplicate, extract_isrc, get_track_isrc, import_playlist
from tests.conftest import FakePlaylistItems, make_item, make_track


SPOTIFY_TRACK = {
    "album": {
        "album_type": "single", "artists": [], "available_markets": [], "external_urls": {},
        "href": "h", "id": "a", "images": [], "name": "A", "release_date": "2025-01-01",
        "release_date_precision": "day", "total_tracks": 1, "type": "album", "uri": "spotify:album:a",
    },
    "artists": [{"external_urls": {}, "href": "h", "id": "x", "name": "X", "type": "artist", "uri": "spotify:artist:x"}],
    "available_markets": [], "disc_number": 1, "duration_ms": 1000, "explicit": False,
    "external_ids": {"isrc": "CA6D22500492"}, "external_urls": {}, "href": "h", "id": "t",
    "is_local": False, "name": "T", "popularity": 1, "preview_url": None, "track_number": 1,
    "type": "track", "uri": "spotify:track:t",
}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_reads_isrc_from_real_tekore_track():
    assert extract_isrc(FullTrack(**SPOTIFY_TRACK)) == "CA6D22500492"


def test_get_track_isrc_returns_code_from_spotify():
    sp = AsyncMock()
    sp.track.return_value = FullTrack(**SPOTIFY_TRACK)
    with patch("app.services.playlists.get_spotify", AsyncMock(return_value=sp)):
        assert run(get_track_isrc("t")) == "CA6D22500492"


def test_import_playlist_saves_isrc(store, fake_pool):
    sp = AsyncMock()
    playlist = MagicMock()
    playlist.name = "TURDOM#1 01/01/2026"
    playlist.tracks.total = 1
    sp.playlist.return_value = playlist
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(FullTrack(**SPOTIFY_TRACK))])

    with patch("app.services.playlists.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.playlists.resolve_and_save_genre", AsyncMock()):
        run(import_playlist(fake_pool, "sp1"))

    assert store._get_track_by_spotify_id("t")["isrc"] == "CA6D22500492"


def test_import_hands_the_isrc_to_the_genre_lookup(store, fake_pool):
    sp = AsyncMock()
    playlist = MagicMock()
    playlist.name = "TURDOM#1 01/01/2026"
    playlist.tracks.total = 1
    sp.playlist.return_value = playlist
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(FullTrack(**SPOTIFY_TRACK))])
    resolve = AsyncMock()

    with patch("app.services.playlists.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.playlists.resolve_and_save_genre", resolve):
        run(import_playlist(fake_pool, "sp1"))

    assert "CA6D22500492" in [*resolve.call_args.args, *resolve.call_args.kwargs.values()]


def test_watcher_checks_duplicates_by_isrc_without_extra_request(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
    sp = AsyncMock()
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(FullTrack(**SPOTIFY_TRACK))])
    check = AsyncMock(return_value=[])
    fallback = AsyncMock(return_value=None)

    with patch("app.services.duplicate_watcher.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.duplicate_watcher.check_duplicate", check), \
            patch("app.services.duplicate_watcher.get_track_isrc", fallback), \
            patch("app.services.duplicate_watcher.resolve_and_save_genre", AsyncMock()), \
            patch("app.services.duplicate_watcher.check_previously_dropped", AsyncMock(return_value=[])):
        run(DuplicateWatcher(fake_pool, AsyncMock())._check_playlists())

    assert check.call_args.args[2] == "CA6D22500492"
    fallback.assert_not_called()


def test_watcher_fills_missing_isrc_on_an_already_known_track(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
    store.add_playlist(id=2, spotify_id="sp2", name="Old", status="listened")
    store.add_track(playlist_id=2, spotify_track_id="t", title="T", artist="X")
    sp = AsyncMock()
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(FullTrack(**SPOTIFY_TRACK))])

    with patch("app.services.duplicate_watcher.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.duplicate_watcher.check_duplicate", AsyncMock(return_value=[])), \
            patch("app.services.duplicate_watcher.resolve_and_save_genre", AsyncMock()), \
            patch("app.services.duplicate_watcher.check_previously_dropped", AsyncMock(return_value=[])):
        run(DuplicateWatcher(fake_pool, AsyncMock())._check_playlists())

    assert store._get_track_by_spotify_id("t")["isrc"] == "CA6D22500492"


def handle_isrc_duplicate(fake_pool, session_status):
    """Run the duplicate handler for an ISRC match; return (spotify, notify, confirm) mocks."""
    sp, notify, confirm = AsyncMock(), AsyncMock(), AsyncMock()
    duplicate = {"match": "isrc", "session_status": session_status, "playlist": "Old", "playlist_id": 2,
                 "title": "Song", "artist": "X", "url": "u", "spotify_track_id": "old"}
    watcher = DuplicateWatcher(fake_pool, notify, confirm_callback=confirm)
    run(watcher._handle_duplicate(sp, {"name": "PL1"}, make_track("new"), "X", [duplicate], None, "sp1", 1))
    return sp, notify, confirm


def test_isrc_match_on_a_kept_track_is_removed(fake_pool):
    sp, notify, confirm = handle_isrc_duplicate(fake_pool, "keep")

    sp.playlist_remove.assert_awaited_once()
    notify.assert_awaited_once()
    confirm.assert_not_called()


def test_isrc_match_on_a_dropped_track_asks_instead_of_removing(fake_pool):
    sp, notify, confirm = handle_isrc_duplicate(fake_pool, "drop")

    sp.playlist_remove.assert_not_called()
    notify.assert_not_called()
    confirm.assert_awaited_once()


def add_old_recording(store):
    store.add_playlist(id=2, spotify_id="sp2", name="Old", status="listened")
    store.add_track(playlist_id=2, spotify_track_id="old", title="Song", artist="X", isrc="I1")


def test_same_recording_on_another_release_is_a_duplicate(store, fake_pool):
    add_old_recording(store)

    found = run(check_duplicate(fake_pool, "new", "I1", title="Song", artist="X"))

    assert [d["match"] for d in found] == ["isrc"]


def test_two_different_versions_sharing_an_isrc_are_not_duplicates(store, fake_pool):
    store.add_playlist(id=2, spotify_id="sp2", name="Old", status="listened")
    store.add_track(playlist_id=2, spotify_track_id="old", title="Song (Slowed)", artist="X", isrc="I1")

    found = run(check_duplicate(fake_pool, "new", "I1", title="Song (Sped Up)", artist="X"))

    assert found == []


def test_modified_version_sharing_an_isrc_is_not_a_duplicate(store, fake_pool):
    add_old_recording(store)

    found = run(check_duplicate(fake_pool, "new", "I1", title="Song (Sped Up)", artist="X"))

    assert found == []
