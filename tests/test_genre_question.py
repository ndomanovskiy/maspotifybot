"""A track whose genre nobody knows: the bot asks whoever added it."""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot import genre_question as question
from app.services.duplicate_watcher import DuplicateWatcher
from app.services.genre_distributor import GENRE_MAP, PICK_TAGS, classify_track_multi
from app.services.genre_resolver import resolve_and_save_genre
from tests.conftest import FakePlaylistItems, make_item, make_track


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_questions_from_other_tests():
    question._questions.clear()
    yield
    question._questions.clear()


def add_playlist_and_ann(store):
    store.add_playlist(id=1, spotify_id="sp1", name="TURDOM#110 <3")
    store.add_user(telegram_id=7, spotify_id="spotify_ann", telegram_username="ann")


SUSPECT = {
    "match": "fuzzy_exact", "session_status": "keep", "playlist": "Old", "playlist_id": 9,
    "title": "Song", "artist": "X", "url": "u", "spotify_track_id": "old",
}


def watch_new_track(fake_pool, resolve, added_by="spotify_ann", times_listed=1, duplicates=(), confirm=None):
    """One watcher pass over a playlist where 'Song' by 'X' was just added; return the question mock."""
    sp = AsyncMock()
    sp.playlist_items.return_value = FakePlaylistItems(
        items=[make_item(make_track("new", name="Song", artist="X"), added_by=added_by)] * times_listed,
    )
    ask = AsyncMock()
    module = "app.services.duplicate_watcher"
    with patch(f"{module}.get_spotify", AsyncMock(return_value=sp)), \
            patch(f"{module}.check_duplicate", AsyncMock(return_value=list(duplicates))), \
            patch(f"{module}.classify_duplicates", AsyncMock(side_effect=lambda pool, found: found)), \
            patch(f"{module}.get_track_isrc", AsyncMock(return_value=None)), \
            patch(f"{module}.resolve_and_save_genre", resolve), \
            patch(f"{module}.check_previously_dropped", AsyncMock(return_value=None)):
        watcher = DuplicateWatcher(fake_pool, AsyncMock(), confirm_callback=confirm, genre_ask_callback=ask)
        run(watcher._check_playlists())
    return ask


def test_nobody_knows_the_genre_so_whoever_added_the_track_is_asked(store, fake_pool):
    add_playlist_and_ann(store)

    ask = watch_new_track(fake_pool, AsyncMock(return_value=None))

    ask.assert_awaited_once_with(
        telegram_id=7, track_db_id=store.get_track(1, "new")["id"], track_title="Song",
        artist="X", track_id="new", playlist_name="TURDOM#110 <3",
    )


def test_a_known_genre_asks_nobody(store, fake_pool):
    add_playlist_and_ann(store)

    ask = watch_new_track(fake_pool, AsyncMock(return_value="rock"))

    ask.assert_not_called()


def test_a_track_added_by_an_unknown_account_asks_nobody(store, fake_pool):
    add_playlist_and_ann(store)

    ask = watch_new_track(fake_pool, AsyncMock(return_value=None), added_by="stranger")

    ask.assert_not_called()


def test_lastfm_or_ai_not_answering_asks_nobody(store, fake_pool):
    from app.services.genre_resolver import GenreLookupFailed
    add_playlist_and_ann(store)

    ask = watch_new_track(fake_pool, AsyncMock(side_effect=GenreLookupFailed("Last.fm did not answer")))

    ask.assert_not_called()


def test_a_track_that_was_not_saved_is_not_asked_about(store, fake_pool):
    add_playlist_and_ann(store)
    answer = store.handle_fetchval

    def saving_fails(query, args):
        if "insert into tracks" in query.lower():
            raise Exception("database is down")
        return answer(query, args)

    with patch.object(store, "handle_fetchval", side_effect=saving_fails):
        ask = watch_new_track(fake_pool, AsyncMock(return_value=None))

    ask.assert_not_called()


def test_a_track_listed_twice_is_asked_about_once(store, fake_pool):
    add_playlist_and_ann(store)

    ask = watch_new_track(fake_pool, AsyncMock(return_value=None), times_listed=2)

    ask.assert_awaited_once()


def test_a_suspected_duplicate_listed_twice_gets_one_question_and_no_genre_question(store, fake_pool):
    add_playlist_and_ann(store)
    confirm = AsyncMock()

    ask = watch_new_track(
        fake_pool, AsyncMock(return_value=None), times_listed=2, duplicates=[SUSPECT], confirm=confirm,
    )

    confirm.assert_awaited_once()
    ask.assert_not_called()
    assert store.get_track(1, "new") is None


# ── The question in a private chat ──────────────────────────────

ANN = 7
GENRES = ["Electronic", "Pop", "Metal", "Rock", "Hip-Hop", "Indie", "R&B", "DnB", "Chill", "Soundtrack", "Phonk"]
_message_ids = itertools.count(1)


def labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def ask_about(track_db_id, send=None):
    """Send the question about one track to Ann; return the send mock."""
    send = send or AsyncMock()
    with patch("app.bot.genre_question.send", send):
        run(question.ask_genre(
            telegram_id=ANN, track_db_id=track_db_id, track_title="Song <3", artist="X & Y",
            track_id="t1", playlist_name="TURDOM#110 <3",
        ))
    return send


def press(data, markup, message_id=None):
    """Build the callback Telegram delivers when Ann taps a button of the question."""
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = ANN
    callback.message.reply_markup = markup
    callback.message.html_text = "🎸 <b>Какой жанр у трека?</b>"
    callback.message.chat.id = ANN
    callback.message.message_id = message_id or next(_message_ids)
    callback.answer = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


def redrawn(callback):
    return callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]


def tap(callback, fake_pool):
    """Handle a tap that may save the genre; return the edit_text mock that closes the question."""
    edit = AsyncMock()
    with patch("app.bot.genre_question.get_pool", return_value=fake_pool), \
            patch("app.bot.genre_question.edit_text", edit):
        run(question.handle_callback(callback))
    return edit


def song(store, genre=None):
    store.add_playlist(id=1, spotify_id="sp1", name="TURDOM#110")
    return store.add_track(playlist_id=1, spotify_track_id="t1", title="Song", genre=genre)


def ticked(track_db_id, *genres, message_id=None):
    """The question's keyboard after Ann ticked the given genres."""
    markup = ask_about(track_db_id).call_args.kwargs["reply_markup"]
    for genre in genres:
        callback = press(f"gnr:t:{track_db_id}:{PICK_TAGS['TURDOM ' + genre]}", markup, message_id)
        run(question.handle_callback(callback))
        markup = redrawn(callback)
    return markup


def test_question_shows_the_track_and_every_genre_unticked():
    send = ask_about(5)

    assert send.call_args.args[0] == ANN
    text = send.call_args.args[1]
    assert "Song &lt;3" in text and "X &amp; Y" in text and "TURDOM#110 &lt;3" in text
    assert labels(send.call_args.kwargs["reply_markup"]) == [*GENRES, "💾 Сохранить", "🤷 Не знаю"]


def test_a_question_telegram_refuses_to_deliver_is_only_logged():
    send = ask_about(5, AsyncMock(side_effect=Exception("bot was blocked by the user")))

    send.assert_awaited_once()


def test_a_database_failure_while_asking_does_not_stop_the_watcher(store, fake_pool):
    add_playlist_and_ann(store)

    with patch.object(store, "handle_fetchrow", side_effect=Exception("database is down")):
        ask = watch_new_track(fake_pool, AsyncMock(return_value=None))

    ask.assert_not_called()
    assert store.get_track(1, "new") is not None


def test_backfill_pauses_after_a_failed_track_too(store, fake_pool):
    from app.services.genre_resolver import GenreLookupFailed, backfill_genres
    song(store)
    sp = AsyncMock()
    sp.track.return_value = make_track("t1")

    with patch("app.services.genre_resolver.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.genre_resolver.resolve_genre",
                  AsyncMock(side_effect=GenreLookupFailed("Last.fm did not answer"))), \
            patch("app.services.genre_resolver.asyncio.sleep", AsyncMock()) as pause:
        result = run(backfill_genres(fake_pool))

    assert result["processed"] == 1
    pause.assert_awaited_once()


def test_tapping_a_genre_ticks_it():
    callback = press("gnr:t:5:hip hop", ask_about(5).call_args.kwargs["reply_markup"])

    run(question.handle_callback(callback))

    assert [label for label in labels(redrawn(callback)) if label.startswith("✅")] == ["✅ Hip-Hop"]
    callback.answer.assert_awaited_once()


def test_tapping_a_ticked_genre_unticks_it():
    callback = press("gnr:t:5:hip hop", ticked(5, "Hip-Hop"))

    run(question.handle_callback(callback))

    assert labels(redrawn(callback))[:len(GENRES)] == GENRES


def test_save_keeps_the_ticked_genres_as_tags_the_distribution_understands(store, fake_pool):
    track = song(store)

    edit = tap(press(f"gnr:save:{track['id']}", ticked(track["id"], "Hip-Hop", "Electronic")), fake_pool)

    assert (track["genre"], track["genre_approved"]) == ("electronic, hip hop", True)
    assert classify_track_multi(track["genre"]) == ["TURDOM Electronic", "TURDOM Hip-Hop"]
    assert edit.call_args.args[2] == "🎸 <b>Какой жанр у трека?</b>\n\n✅ Жанр: Electronic, Hip-Hop"


def test_a_picked_genre_the_hierarchy_leaves_out_is_explained(store, fake_pool):
    track = song(store)

    edit = tap(press(f"gnr:save:{track['id']}", ticked(track["id"], "Metal", "Rock")), fake_pool)

    assert track["genre"] == "metal, rock"
    assert classify_track_multi(track["genre"]) == ["TURDOM Metal"]
    assert edit.call_args.args[2].endswith("✅ Жанр: Metal (Rock не добавлен: с Metal трек в Rock не идёт)")


def test_the_explanation_holds_when_more_genres_are_picked(store, fake_pool):
    track = song(store)

    edit = tap(press(f"gnr:save:{track['id']}", ticked(track["id"], "Metal", "Rock", "Hip-Hop")), fake_pool)

    assert edit.call_args.args[2].endswith("✅ Жанр: Metal, Hip-Hop (Rock не добавлен: с Metal трек в Rock не идёт)")


def test_genre_names_in_the_answer_are_escaped(store, fake_pool):
    track = song(store)

    edit = tap(press(f"gnr:save:{track['id']}", ticked(track["id"], "Pop", "R&B")), fake_pool)

    assert edit.call_args.args[2].endswith("✅ Жанр: R&amp;B (Pop не добавлен: с R&amp;B трек в Pop не идёт)")


def test_save_with_nothing_ticked_asks_to_tick_and_saves_nothing(store, fake_pool):
    track = song(store)
    callback = press(f"gnr:save:{track['id']}", ask_about(track["id"]).call_args.kwargs["reply_markup"])

    edit = tap(callback, fake_pool)

    assert track["genre"] is None
    edit.assert_not_called()
    assert callback.answer.call_args.kwargs.get("show_alert") is True


def test_dont_know_saves_nothing_and_closes_the_question(store, fake_pool):
    track = song(store)

    edit = tap(press(f"gnr:skip:{track['id']}", ticked(track["id"], "Rock")), fake_pool)

    assert track["genre"] is None
    assert "без жанра" in edit.call_args.args[2]


def test_a_genre_saved_meanwhile_is_not_replaced(store, fake_pool):
    track = song(store, genre="rock")

    edit = tap(press(f"gnr:save:{track['id']}", ticked(track["id"], "Pop")), fake_pool)

    assert track["genre"] == "rock"
    assert "уже указан" in edit.call_args.args[2]


def test_double_tap_on_save_closes_the_question_once(store, fake_pool):
    track = song(store)
    markup = ticked(track["id"], "Rock", message_id=9000)

    first = tap(press(f"gnr:save:{track['id']}", markup, message_id=9000), fake_pool)
    second = tap(press(f"gnr:save:{track['id']}", markup, message_id=9000), fake_pool)

    first.assert_awaited_once()
    second.assert_not_called()
    assert track["genre"] == "rock"


def test_double_tap_on_save_at_the_same_moment_saves_once(store, fake_pool):
    track = song(store)
    markup = ticked(track["id"], "Rock", message_id=9050)

    async def slow_save(pool, track_db_id, genre):
        await asyncio.sleep(0)  # the second tap gets its turn here
        return True

    async def both_taps():
        await asyncio.gather(
            question.handle_callback(press(f"gnr:save:{track['id']}", markup, message_id=9050)),
            question.handle_callback(press(f"gnr:save:{track['id']}", markup, message_id=9050)),
        )

    with patch("app.bot.genre_question.save_genre_if_missing", AsyncMock(side_effect=slow_save)) as saving, \
            patch("app.bot.genre_question.get_pool", return_value=fake_pool), \
            patch("app.bot.genre_question.edit_text", AsyncMock()):
        run(both_taps())

    saving.assert_awaited_once()


def test_a_failed_redraw_does_not_undo_a_later_tick():
    markup = ask_about(5).call_args.kwargs["reply_markup"]
    first = press("gnr:t:5:rock", markup, message_id=9060)
    second = press("gnr:t:5:pop", markup, message_id=9060)

    async def second_tap_then_failure(**kwargs):
        await question.handle_callback(second)  # draws Rock and Pop while the first redraw hangs
        raise Exception("network timeout")

    first.message.edit_reply_markup = AsyncMock(side_effect=second_tap_then_failure)
    run(question.handle_callback(first))

    assert not first.answer.call_args.args  # the later tap already shows this tick: nothing to tap again
    third = press("gnr:t:5:indie", markup, message_id=9060)
    run(question.handle_callback(third))
    assert [label for label in labels(redrawn(third)) if label.startswith("✅")] == ["✅ Pop", "✅ Rock", "✅ Indie"]


def test_a_failed_redraw_asks_to_tap_again_and_forgets_the_unseen_tick():
    markup = ask_about(5).call_args.kwargs["reply_markup"]
    failed = press("gnr:t:5:rock", markup, message_id=9070)
    failed.message.edit_reply_markup.side_effect = Exception("network timeout")

    run(question.handle_callback(failed))

    assert "ещё раз" in failed.answer.call_args.args[0]
    retry = press("gnr:t:5:pop", markup, message_id=9070)
    run(question.handle_callback(retry))
    assert [label for label in labels(redrawn(retry)) if label.startswith("✅")] == ["✅ Pop"]


def test_a_question_telegram_no_longer_shows_the_bot_says_so(fake_pool):
    from aiogram.types import Chat, InaccessibleMessage

    callback = press("gnr:save:5", None)
    callback.message = InaccessibleMessage(chat=Chat(id=ANN, type="private"), message_id=1, date=0)

    edit = tap(callback, fake_pool)

    edit.assert_not_called()
    assert "устарел" in callback.answer.call_args.args[0]


def test_question_passes_its_keyboard_through_the_real_send_helper():
    from app.bot import core
    send_message = AsyncMock()

    with patch.object(core.bot, "send_message", send_message):
        run(question.ask_genre(
            telegram_id=ANN, track_db_id=5, track_title="Song", artist="X", track_id="t1", playlist_name="PL",
        ))

    assert send_message.call_args.args[0] == ANN
    assert send_message.call_args.kwargs["parse_mode"] == "HTML"
    assert labels(send_message.call_args.kwargs["reply_markup"])[-2:] == ["💾 Сохранить", "🤷 Не знаю"]


def test_unknown_button_changes_nothing(store, fake_pool):
    callback = press("gnr:foo:5", ask_about(5).call_args.kwargs["reply_markup"])

    edit = tap(callback, fake_pool)

    edit.assert_not_called()
    callback.message.edit_reply_markup.assert_not_called()
    callback.answer.assert_awaited_once()


def test_second_tap_carrying_a_stale_keyboard_keeps_the_first_tick():
    markup = ask_about(5).call_args.kwargs["reply_markup"]
    run(question.handle_callback(press("gnr:t:5:rock", markup, message_id=9100)))

    second = press("gnr:t:5:pop", markup, message_id=9100)
    run(question.handle_callback(second))

    assert [label for label in labels(redrawn(second)) if label.startswith("✅")] == ["✅ Pop", "✅ Rock"]


def test_failed_save_keeps_the_question_open_for_another_try(store, fake_pool):
    track = song(store)
    markup = ticked(track["id"], "Rock", message_id=9200)
    failed = press(f"gnr:save:{track['id']}", markup, message_id=9200)

    with patch.object(store, "handle_fetchval", side_effect=Exception("database is down")):
        tap(failed, fake_pool)

    assert failed.answer.call_args.kwargs.get("show_alert") is True
    tap(press(f"gnr:save:{track['id']}", markup, message_id=9200), fake_pool)
    assert track["genre"] == "rock"


def test_the_track_comes_from_the_question_not_from_the_tap(store, fake_pool):
    track = song(store)
    other = store.add_track(playlist_id=1, spotify_track_id="t2", title="Other")

    tap(press(f"gnr:save:{other['id']}", ticked(track["id"], "Rock")), fake_pool)

    assert (track["genre"], other["genre"]) == ("rock", None)


def test_tap_on_a_closed_question_does_nothing(fake_pool):
    callback = press("gnr:save:5", None)

    edit = tap(callback, fake_pool)

    edit.assert_not_called()
    callback.answer.assert_awaited_once()


def test_unknown_genre_in_a_tap_changes_nothing():
    callback = press("gnr:t:5:polka", ask_about(5).call_args.kwargs["reply_markup"])

    run(question.handle_callback(callback))

    callback.message.edit_reply_markup.assert_not_called()
    callback.answer.assert_awaited_once()


def test_repeat_tap_that_changes_nothing_is_harmless():
    callback = press("gnr:t:5:rock", ask_about(5).call_args.kwargs["reply_markup"])
    callback.message.edit_reply_markup.side_effect = TelegramBadRequest(
        method=MagicMock(), message="message is not modified",
    )

    run(question.handle_callback(callback))

    callback.answer.assert_awaited_once()


ADMIN = 100


def test_reset_of_all_genres_keeps_the_ones_people_picked(store, fake_pool):
    from app.bot.commands import admin
    found = song(store, genre="rock")
    picked = store.add_track(playlist_id=1, spotify_track_id="t2", title="Picked", genre="pop")
    picked["genre_approved"] = True
    message = MagicMock()
    message.text = "/backfill_genres reset"
    message.from_user.id = ADMIN
    message.answer = AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.commands.admin.get_pool", return_value=fake_pool), \
            patch("app.bot.commands.admin.backfill_genres", AsyncMock(return_value={"processed": 0, "resolved": 0, "unknown": 0})), \
            patch("app.bot.commands.admin.reply", AsyncMock()):
        settings.telegram_admin_id = ADMIN
        run(admin.on_backfill_genres(message))

    assert (found["genre"], picked["genre"]) == (None, "pop")


def test_backfill_does_not_overwrite_a_genre_picked_while_it_runs(store, fake_pool):
    from app.services.genre_resolver import backfill_genres
    track = song(store)

    def picked_meanwhile(_track):
        track["genre"], track["genre_approved"] = "rock", True
        return "pop"

    sp = AsyncMock()
    sp.track.return_value = make_track("t1")
    with patch("app.services.genre_resolver.get_spotify", AsyncMock(return_value=sp)), \
            patch("app.services.genre_resolver.resolve_genre", AsyncMock(side_effect=picked_meanwhile)):
        result = run(backfill_genres(fake_pool))

    assert (result["processed"], result["resolved"]) == (1, 0)
    assert track["genre"] == "rock"


def tap_as(registered: bool):
    """Tap a genre button as a member or a stranger; return (callback, handler mock)."""
    from app.bot import callbacks

    callback = press("gnr:t:5:rock", MagicMock())
    callback.from_user.id = 999
    handler = AsyncMock()
    with patch("app.bot.callbacks.is_registered", AsyncMock(return_value=registered)), \
            patch("app.bot.callbacks.handle_genre_question", handler):
        run(callbacks.on_genre_question(callback))
    return callback, handler


def test_a_member_can_answer_the_question():
    callback, handler = tap_as(registered=True)

    handler.assert_awaited_once_with(callback)


def test_a_stranger_cannot_answer_the_question():
    callback, handler = tap_as(registered=False)

    handler.assert_not_called()
    assert callback.answer.call_args.kwargs.get("show_alert") is True


def test_a_saved_genre_is_kept_and_not_looked_up_again(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1")
    store.add_track(playlist_id=1, spotify_track_id="t1", title="Song", genre="rock")
    lookup = AsyncMock(return_value="pop")

    with patch("app.services.genre_resolver.resolve_genre", lookup):
        genre = run(resolve_and_save_genre(fake_pool, make_track("t1")))

    assert genre == "rock"
    lookup.assert_not_called()


def test_a_found_genre_is_saved_for_a_track_without_one(store, fake_pool):
    store.add_playlist(id=1, spotify_id="sp1", name="PL1")
    store.add_track(playlist_id=1, spotify_track_id="t1", title="Song")

    with patch("app.services.genre_resolver.resolve_genre", AsyncMock(return_value="hip hop")):
        genre = run(resolve_and_save_genre(fake_pool, make_track("t1")))

    assert genre == "hip hop"
    assert store.get_track(1, "t1")["genre"] == "hip hop"


def test_every_genre_playlist_can_be_picked_and_its_tag_leads_back_to_it():
    assert list(PICK_TAGS) == list(GENRE_MAP)
    for playlist, tag in PICK_TAGS.items():
        assert classify_track_multi(tag) == [playlist]
