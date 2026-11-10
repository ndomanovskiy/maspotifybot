"""Pressing «Оставить» on a suspected duplicate holds for that one playlist."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, InaccessibleMessage, Message

from app.services.duplicate_watcher import DuplicateWatcher
from app.services.normalize import normalize_artist, normalize_title
from app.services.playlists import check_duplicate, mark_duplicate_kept
from tests.conftest import FakePlaylistItems, make_item, make_track, telegram_message

SUSPECT = {
    "match": "fuzzy_exact", "session_status": "keep", "playlist": "Old", "playlist_id": 9,
    "title": "Song", "artist": "X", "url": "u", "spotify_track_id": "old",
}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def add_open_playlist(store, playlist_id=1, spotify_id="sp1"):
    store.add_playlist(id=playlist_id, spotify_id=spotify_id, name=f"PL{playlist_id}", status="upcoming")


def watch(fake_pool, dropped=None, duplicates=None, isrc=None):
    """Run one watcher pass over a playlist holding one suspect track; return (confirm, drop_warn)."""
    sp = AsyncMock()
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(make_track("new", name="Song", artist="X"))])
    confirm, drop_warn = AsyncMock(), AsyncMock()
    module = "app.services.duplicate_watcher"
    with patch(f"{module}.get_spotify", AsyncMock(return_value=sp)), \
            patch(f"{module}.check_duplicate", AsyncMock(return_value=[dict(SUSPECT)] if duplicates is None else duplicates)), \
            patch(f"{module}.classify_duplicates", AsyncMock(side_effect=lambda pool, found: found)), \
            patch(f"{module}.get_track_isrc", AsyncMock(return_value=isrc)), \
            patch(f"{module}.resolve_and_save_genre", AsyncMock()), \
            patch(f"{module}.check_previously_dropped", AsyncMock(return_value=dropped or [])):
        watcher = DuplicateWatcher(fake_pool, AsyncMock(), confirm_callback=confirm, drop_warn_callback=drop_warn)
        run(watcher._check_playlists())
    return confirm, drop_warn


def test_kept_track_is_not_asked_about_again_and_is_saved(store, fake_pool):
    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))

    confirm, _ = watch(fake_pool)

    confirm.assert_not_called()
    assert store.get_track(1, "new") is not None
    assert store.kept_duplicates == [{"playlist_id": 1, "spotify_track_id": "new", "kept_by": 5}]


def add_song(store, playlist_id, track_id, isrc=None):
    """Save 'Song' by 'X' the way the watcher does, normalized fields included."""
    track = store.add_track(playlist_id=playlist_id, spotify_track_id=track_id, title="Song", artist="X", isrc=isrc)
    track["normalized_title"], track["normalized_artist"] = normalize_title("Song"), normalize_artist("X")


def lookalike_of_a_listened_track(store, fake_pool, copy_status, kept):
    """'new' sounds like 'old' from a listened playlist and has a copy in playlist 1."""
    store.add_playlist(id=9, spotify_id="old_pl", name="Old", status="listened")
    add_song(store, 9, "old")
    playlist = store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming")
    add_song(store, 1, "new")
    if kept:
        run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))
    playlist["status"] = copy_status  # a playlist may close after someone pressed «Оставить»


def matches(fake_pool):
    found = run(check_duplicate(fake_pool, "new", None, title="Song", artist="X"))
    return [(d["match"], d["playlist_id"]) for d in found]


def test_copy_kept_in_an_open_playlist_does_not_hide_the_lookalike(store, fake_pool):
    lookalike_of_a_listened_track(store, fake_pool, "upcoming", kept=True)

    assert matches(fake_pool) == [("exact", 1), ("fuzzy_exact", 9)]


def test_same_recording_kept_in_an_open_playlist_does_not_hide_the_lookalike(store, fake_pool):
    store.add_playlist(id=9, spotify_id="old_pl", name="Old", status="listened")
    add_song(store, 9, "old")
    add_open_playlist(store)
    add_song(store, 1, "kept", isrc="I1")
    run(mark_duplicate_kept(fake_pool, "sp1", "kept", 5))

    found = run(check_duplicate(fake_pool, "rerelease", "I1", title="Song", artist="X"))

    assert [(d["match"], d["playlist_id"]) for d in found] == [("isrc", 1), ("fuzzy_exact", 9)]


def test_copy_in_an_open_playlist_that_was_not_kept_is_still_an_exact_match(store, fake_pool):
    lookalike_of_a_listened_track(store, fake_pool, "upcoming", kept=False)

    assert matches(fake_pool) == [("exact", 1)]


def test_copy_kept_in_a_listened_playlist_is_still_an_exact_match(store, fake_pool):
    lookalike_of_a_listened_track(store, fake_pool, "listened", kept=True)

    assert store.kept_duplicates != []
    assert matches(fake_pool) == [("exact", 1)]


def test_kept_copy_comes_first_and_a_played_copy_still_stops_the_search(store, fake_pool):
    lookalike_of_a_listened_track(store, fake_pool, "upcoming", kept=True)
    add_song(store, 9, "new")  # the same track was also played in the listened playlist

    assert matches(fake_pool) == [("exact", 1), ("exact", 9)]


def test_kept_track_is_saved_with_the_isrc_spotify_left_out(store, fake_pool):
    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))

    watch(fake_pool, isrc="I9")

    assert store.get_track(1, "new")["isrc"] == "I9"


def test_thematic_track_is_saved_with_the_isrc_spotify_left_out(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="upcoming", is_thematic=True)

    watch(fake_pool, isrc="I9")

    assert store.get_track(1, "new")["isrc"] == "I9"


def test_keeping_in_one_playlist_does_not_cover_another(store, fake_pool):
    add_open_playlist(store, 1, "sp1")
    add_open_playlist(store, 2, "sp2")
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))

    confirm, _ = watch(fake_pool)

    assert confirm.await_count == 1
    assert confirm.call_args.kwargs["playlist_spotify_id"] == "sp2"


def test_kept_track_is_not_warned_about_as_previously_dropped(store, fake_pool):
    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))

    _, drop_warn = watch(fake_pool, dropped=[{"playlist": "Old", "date": "d", "added_by": "a"}])

    drop_warn.assert_not_called()


def press(data, telegram_id=5):
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = telegram_id
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


def keep(callback, fake_pool, registered=True):
    from app.bot import callbacks

    with patch("app.bot.callbacks.get_pool", return_value=fake_pool), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=registered)):
        run(callbacks.on_keep_dup(callback))


def test_keep_button_remembers_the_track_for_that_playlist(store, fake_pool):
    from app.bot import callbacks

    add_open_playlist(store)
    callback = press(callbacks.keep_duplicate_button("sp1", "new").callback_data)

    keep(callback, fake_pool)

    assert store.kept_duplicates == [{"playlist_id": 1, "spotify_track_id": "new", "kept_by": 5}]
    callback.message.edit_text.assert_awaited_once()


def test_keeping_in_an_unknown_playlist_stores_nothing_and_closes_the_question(store, fake_pool):
    callback = press("keep_dup:gone:new")

    keep(callback, fake_pool)

    assert store.kept_duplicates == []
    callback.answer.assert_awaited_once_with("✅ Оставлено")
    callback.message.edit_text.assert_awaited_once()


def test_keeping_in_a_closed_playlist_stores_nothing_and_closes_the_question(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1", status="listened")
    callback = press("keep_dup:sp1:new")

    keep(callback, fake_pool)

    assert store.kept_duplicates == []
    callback.message.edit_text.assert_awaited_once()


def test_keep_works_on_a_message_telegram_no_longer_shows(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    callback.message = InaccessibleMessage(chat=Chat(id=1, type="private"), message_id=1, date=0)

    keep(callback, fake_pool)

    assert store.kept_duplicates == [{"playlist_id": 1, "spotify_track_id": "new", "kept_by": 5}]
    callback.answer.assert_awaited_once_with("✅ Оставлено")


def test_button_sent_before_this_change_still_answers(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:new")

    keep(callback, fake_pool)

    assert store.kept_duplicates == []
    assert "может спросить снова" in callback.answer.call_args.args[0]
    callback.message.edit_text.assert_awaited_once()


def test_outsider_cannot_keep_a_track(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new", telegram_id=999)

    keep(callback, fake_pool, registered=False)

    assert store.kept_duplicates == []
    callback.message.edit_text.assert_not_called()


def test_failed_save_tells_the_user_and_leaves_the_question_open(store, fake_pool):
    from app.bot import callbacks

    callback = press("keep_dup:sp1:new")

    with patch("app.bot.callbacks.get_pool", return_value=fake_pool), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=True)), \
            patch("app.bot.callbacks.mark_duplicate_kept", AsyncMock(side_effect=Exception("db is down"))):
        run(callbacks.on_keep_dup(callback))

    assert callback.answer.call_args.kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_called()
    assert callback.message.edit_reply_markup.call_args.kwargs["reply_markup"] is callback.message.reply_markup


def test_keep_takes_the_buttons_away_before_saving(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    saved_when_buttons_went = []
    callback.message.edit_reply_markup.side_effect = \
        lambda **_: saved_when_buttons_went.append(list(store.kept_duplicates))

    keep(callback, fake_pool)

    assert saved_when_buttons_went == [[]]
    assert store.kept_duplicates != []


def scan(fake_pool):
    """Run /scan over the open playlist holding one suspect track; return (duplicate check mock, report)."""
    from app.bot.commands import admin

    sp = AsyncMock()
    sp.playlist_items.return_value = FakePlaylistItems(items=[make_item(make_track("new", name="Song", artist="X"))])
    message = MagicMock()
    message.answer = AsyncMock()
    message.from_user.id = 100
    check = AsyncMock(return_value=[])

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.commands.admin.get_pool", return_value=fake_pool), \
            patch("app.bot.commands.admin.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.bot.commands.admin.get_track_isrc", AsyncMock(return_value=None)), \
            patch("app.bot.commands.admin.check_duplicate", check), \
            patch("app.bot.commands.admin.reply", AsyncMock()):
        settings.telegram_admin_id = 100
        run(admin.cmd_scan(message))
    return check, message.answer.call_args.args[0]


def test_scan_leaves_a_kept_track_alone_and_says_so(store, fake_pool):
    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))

    check, report = scan(fake_pool)

    check.assert_not_called()
    assert "Оставлено ранее: 1" in report


def test_scan_checks_a_track_that_was_not_kept(store, fake_pool):
    add_open_playlist(store)

    check, report = scan(fake_pool)

    check.assert_awaited_once()
    assert "Оставлено ранее" not in report


def test_keep_button_with_broken_data_says_error(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new:extra")

    keep(callback, fake_pool)

    callback.answer.assert_awaited_once_with("Ошибка")
    assert store.kept_duplicates == []
    callback.message.edit_text.assert_not_called()


def test_track_that_was_not_kept_gets_the_drop_warning(store, fake_pool):
    add_open_playlist(store)

    _, drop_warn = watch(fake_pool, dropped=[{"playlist": "Old", "date": "d", "added_by": "a"}], duplicates=[])

    drop_warn.assert_awaited_once()


def test_removing_a_track_forgets_that_it_was_kept(store, fake_pool):
    from app.bot import callbacks

    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "new", 5))
    callback = press("confirm_dup:sp1:new")

    with patch("app.bot.callbacks.get_pool", return_value=fake_pool), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=True)), \
            patch("app.spotify.auth.get_spotify", AsyncMock(return_value=AsyncMock())):
        run(callbacks.on_confirm_dup(callback))

    assert store.kept_duplicates == []


def test_failed_removal_brings_the_buttons_back(store, fake_pool):
    from app.bot import callbacks

    add_open_playlist(store)
    callback = press("confirm_dup:sp1:new")
    spotify = AsyncMock()
    spotify.playlist_remove.side_effect = Exception("spotify is down")

    with patch("app.bot.callbacks.get_pool", return_value=fake_pool), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=True)), \
            patch("app.spotify.auth.get_spotify", AsyncMock(return_value=spotify)):
        run(callbacks.on_confirm_dup(callback))

    spotify.playlist_remove.assert_awaited_once()
    assert callback.answer.call_args.kwargs.get("show_alert") is True
    assert callback.message.edit_reply_markup.call_args.kwargs["reply_markup"] is callback.message.reply_markup


def test_second_tap_on_a_closed_question_is_not_a_warning(store, fake_pool, caplog):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(), message="Bad Request: message is not modified",
    )

    with caplog.at_level(logging.DEBUG, logger="app.bot.callbacks"):
        keep(callback, fake_pool)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_failing_to_close_a_question_is_a_warning(store, fake_pool, caplog):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(), message="Bad Request: can't parse entities",
    )

    with caplog.at_level(logging.DEBUG, logger="app.bot.callbacks"):
        keep(callback, fake_pool)

    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [logging.WARNING]


def test_keep_survives_telegram_refusing_a_late_answer(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    callback.answer.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad Request: query is too old")

    keep(callback, fake_pool)

    assert store.kept_duplicates != []
    callback.message.edit_text.assert_awaited_once()


def test_failed_tidy_up_after_removal_still_reports_removed(store, fake_pool):
    from app.bot import callbacks

    add_open_playlist(store)
    callback = press("confirm_dup:sp1:new")

    with patch("app.bot.callbacks.get_pool", side_effect=Exception("db is down")), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=True)), \
            patch("app.spotify.auth.get_spotify", AsyncMock(return_value=AsyncMock())):
        run(callbacks.on_confirm_dup(callback))

    callback.answer.assert_awaited_once_with("🗑 Удалено!")
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    callback.message.edit_text.assert_awaited_once()


def test_watcher_forgets_a_kept_track_that_left_the_playlist(store, fake_pool):
    add_open_playlist(store)
    run(mark_duplicate_kept(fake_pool, "sp1", "gone", 5))

    watch(fake_pool)

    assert store.kept_duplicates == []


def assert_formatted_without_preview(edit):
    assert edit.call_args.args[0].startswith("I &lt;3 <b>Song</b>")
    assert edit.call_args.kwargs["link_preview_options"].is_disabled is True


def test_keep_confirmation_keeps_formatting_and_escapes_text(store, fake_pool):
    add_open_playlist(store)
    callback = press("keep_dup:sp1:new")
    callback.message = telegram_message("I <3 Song", "Song")

    with patch.object(Message, "edit_text", AsyncMock()) as edit, \
            patch.object(Message, "edit_reply_markup", AsyncMock()):
        keep(callback, fake_pool)

    assert_formatted_without_preview(edit)


def test_remove_confirmation_keeps_formatting_and_escapes_text(store, fake_pool):
    from app.bot import callbacks

    add_open_playlist(store)
    callback = press("confirm_dup:sp1:new")
    callback.message = telegram_message("I <3 Song", "Song")

    with patch.object(Message, "edit_text", AsyncMock()) as edit, \
            patch.object(Message, "edit_reply_markup", AsyncMock()), \
            patch("app.bot.callbacks.get_pool", return_value=fake_pool), \
            patch("app.bot.callbacks.is_registered", AsyncMock(return_value=True)), \
            patch("app.spotify.auth.get_spotify", AsyncMock(return_value=AsyncMock())):
        run(callbacks.on_confirm_dup(callback))

    assert_formatted_without_preview(edit)
