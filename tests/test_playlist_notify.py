"""Admin picker for who gets told about a new playlist."""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

from app.bot import playlist_notify as notify

ADMIN = 100
PLAYLIST = {"number": 110, "name": "TURDOM#110 11/11/2026", "url": "https://open.spotify.com/playlist/p"}
_message_ids = itertools.count(1)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def offer(fake_pool):
    """Run offer_notification and return the keyboard it sent."""
    send = AsyncMock()
    with patch("app.bot.playlist_notify.get_pool", return_value=fake_pool), \
            patch("app.bot.playlist_notify.send", send), \
            patch("app.bot.playlist_notify.settings") as settings:
        settings.telegram_admin_id = ADMIN
        run(notify.offer_notification(PLAYLIST))
    return send


def press(data, markup, message_id=None):
    """Build the callback Telegram delivers when the admin taps a button."""
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = ADMIN
    callback.message.reply_markup = markup
    callback.message.chat.id = ADMIN
    callback.message.message_id = message_id or next(_message_ids)
    callback.answer = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


def picker(store, fake_pool):
    add_people(store)
    return offer(fake_pool).call_args.kwargs["reply_markup"]


def redrawn(callback):
    return callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]


def add_people(store):
    store.add_user(telegram_id=2, spotify_id="b", telegram_name="Bob")
    store.add_user(telegram_id=1, spotify_id="a", telegram_username="ann")
    store.add_user(telegram_id=ADMIN, spotify_id="me", telegram_username="boss")


def test_offer_lists_everyone_except_admin_with_nothing_ticked(store, fake_pool):
    add_people(store)

    send = offer(fake_pool)

    assert send.call_args.args[0] == ADMIN
    assert labels(send.call_args.kwargs["reply_markup"]) == [
        "⬜ @ann", "⬜ Bob", "Выбрать всех", "📣 Отправить (0)", "Не отправлять",
    ]


def test_tap_ticks_only_that_person(store, fake_pool):
    callback = press("pln:t:110:2", picker(store, fake_pool))

    run(notify.handle_callback(callback))

    assert labels(redrawn(callback)) == [
        "⬜ @ann", "✅ Bob", "Выбрать всех", "📣 Отправить (1)", "Не отправлять",
    ]


def test_select_all_ticks_everyone_and_offers_to_clear(store, fake_pool):
    callback = press("pln:all:110", picker(store, fake_pool))

    run(notify.handle_callback(callback))

    assert labels(redrawn(callback)) == [
        "✅ @ann", "✅ Bob", "Снять всех", "📣 Отправить (2)", "Не отправлять",
    ]


def ticked_markup(store, fake_pool, *telegram_ids):
    """A picker with the given people ticked and the playlist present in the DB."""
    store.add_playlist(id=1, spotify_id="p", name=PLAYLIST["name"], number=110, url=PLAYLIST["url"])
    markup = picker(store, fake_pool)
    for telegram_id in telegram_ids:
        callback = press(f"pln:t:110:{telegram_id}", markup)
        run(notify.handle_callback(callback))
        markup = redrawn(callback)
    return markup


def tap(callback, fake_pool, send=None):
    """Handle a tap that may send messages; return (send mock, edit_text mock)."""
    send = send or AsyncMock()
    edit = AsyncMock()
    with patch("app.bot.playlist_notify.get_pool", return_value=fake_pool), \
            patch("app.bot.playlist_notify.send", send), \
            patch("app.bot.playlist_notify.edit_text", edit):
        run(notify.handle_callback(callback))
    return send, edit


def test_send_delivers_only_to_ticked_people_and_reports(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 2)

    send, edit = tap(press("pln:send:110", markup), fake_pool)

    assert [call.args[0] for call in send.call_args_list] == [2]
    assert PLAYLIST["url"] in send.call_args.args[1]
    assert "Bob" in edit.call_args.args[2]
    assert "ann" not in edit.call_args.args[2]


def test_failed_delivery_does_not_stop_others_and_is_reported(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 1, 2)
    send = AsyncMock(side_effect=[Exception("bot was blocked"), None])

    _, edit = tap(press("pln:send:110", markup), fake_pool, send)

    assert send.call_count == 2
    report = edit.call_args.args[2]
    assert "Не дошло: @ann" in report
    assert "Получили: Bob" in report


def test_double_tap_on_send_delivers_once(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 2)
    send = AsyncMock()

    tap(press("pln:send:110", markup, message_id=5000), fake_pool, send)
    tap(press("pln:send:110", markup, message_id=5000), fake_pool, send)

    assert send.call_count == 1


def test_send_with_nobody_ticked_warns_and_keeps_the_picker(store, fake_pool):
    callback = press("pln:send:110", ticked_markup(store, fake_pool))

    send, edit = tap(callback, fake_pool)

    send.assert_not_called()
    edit.assert_not_called()
    assert callback.answer.call_args.kwargs.get("show_alert") is True


def test_skip_sends_nothing_and_closes_the_picker(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 2)

    send, edit = tap(press("pln:skip:110", markup), fake_pool)

    send.assert_not_called()
    assert "не отправлено" in edit.call_args.args[2]


def test_playlist_name_and_people_are_html_escaped(store, fake_pool):
    store.add_playlist(id=1, spotify_id="p", name="TURDOM#110 - R&B <3", number=110, url=PLAYLIST["url"])
    store.add_user(telegram_id=1, spotify_id="a", telegram_name="A<b>")
    tick = press("pln:t:110:1", offer(fake_pool).call_args.kwargs["reply_markup"])
    run(notify.handle_callback(tick))

    send, edit = tap(press("pln:send:110", redrawn(tick)), fake_pool)

    assert "R&amp;B &lt;3" in send.call_args.args[1]
    assert "A&lt;b&gt;" in edit.call_args.args[2]


def test_tap_on_a_closed_picker_does_nothing(store, fake_pool):
    callback = press("pln:send:110", None)

    send, edit = tap(callback, fake_pool)

    send.assert_not_called()
    edit.assert_not_called()
    callback.answer.assert_awaited_once()


def test_only_admin_can_use_the_picker():
    from app.bot import callbacks

    callback = press("pln:send:110", MagicMock())
    callback.from_user.id = 999
    handler = AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.callbacks.handle_playlist_notify", handler):
        settings.telegram_admin_id = ADMIN
        run(callbacks.on_playlist_notify(callback))

    handler.assert_not_called()
    callback.answer.assert_awaited_once()


def test_create_button_offers_picker_and_notifies_nobody():
    from app.bot import callbacks

    callback = MagicMock()
    callback.data = "create_playlist:normal"
    callback.from_user.id = ADMIN
    callback.message.chat.id = ADMIN
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    created = {**PLAYLIST, "auto_closed": []}
    send, offer_picker = AsyncMock(), AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.callbacks.create_next_playlist", AsyncMock(return_value=created)), \
            patch("app.bot.callbacks.get_pool"), \
            patch("app.bot.callbacks.send", send), \
            patch("app.bot.callbacks.offer_notification", offer_picker), \
            patch("app.bot.callbacks.session") as session:
        settings.telegram_admin_id = ADMIN
        session.participants = [1, 2]
        run(callbacks.on_create_playlist(callback))

    send.assert_not_called()
    offer_picker.assert_awaited_once_with(created)


def test_playlist_create_command_offers_picker_and_notifies_nobody():
    from app.bot.commands import admin

    message = MagicMock()
    message.text = "/playlist create"
    message.from_user.id = ADMIN
    message.chat.id = ADMIN
    created = {"status": "ok", "message": "done", "playlist": dict(PLAYLIST)}
    send, offer_picker = AsyncMock(), AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.commands.admin.cmd_create_next", AsyncMock(return_value=created)), \
            patch("app.bot.commands.admin.get_pool"), \
            patch("app.bot.commands.admin.reply", AsyncMock()), \
            patch("app.bot.commands.admin.send", send), \
            patch("app.bot.commands.admin.offer_notification", offer_picker):
        settings.telegram_admin_id = ADMIN
        run(admin.cmd_playlist(message))

    send.assert_not_called()
    offer_picker.assert_awaited_once_with(created["playlist"])


def test_theme_input_offers_picker_and_notifies_nobody():
    import app.bot as bot_package

    message = MagicMock()
    message.text = "Rock"
    message.from_user.id = ADMIN
    message.chat.id = ADMIN
    created = {**PLAYLIST, "auto_closed": []}
    send, offer_picker = AsyncMock(), AsyncMock()

    with patch("app.bot.session") as session, \
            patch("app.services.playlists.create_next_playlist", AsyncMock(return_value=created)), \
            patch("app.bot.core.get_pool"), \
            patch("app.bot.reply", AsyncMock()), \
            patch("app.bot.send", send), \
            patch("app.bot.offer_notification", offer_picker):
        session.waiting_theme = True
        session.participants = [1, 2]
        run(bot_package.on_theme_input(message))

    send.assert_not_called()
    offer_picker.assert_awaited_once_with(created)


def test_repeat_tap_that_changes_nothing_is_harmless(store, fake_pool):
    callback = press("pln:t:110:2", picker(store, fake_pool))
    callback.message.edit_reply_markup.side_effect = TelegramBadRequest(
        method=MagicMock(), message="message is not modified",
    )

    run(notify.handle_callback(callback))

    callback.answer.assert_awaited_once()


def test_no_picker_when_there_is_nobody_else(store, fake_pool):
    store.add_user(telegram_id=ADMIN, spotify_id="me", telegram_username="boss")

    send = offer(fake_pool)

    send.assert_not_called()


def test_picker_failure_does_not_break_playlist_creation(store, fake_pool):
    add_people(store)
    send = AsyncMock(side_effect=Exception("telegram is down"))

    with patch("app.bot.playlist_notify.get_pool", return_value=fake_pool), \
            patch("app.bot.playlist_notify.send", send), \
            patch("app.bot.playlist_notify.settings") as settings:
        settings.telegram_admin_id = ADMIN
        run(notify.offer_notification(PLAYLIST))

    assert send.await_count == 2
    assert send.call_args.args[0] == ADMIN
    assert "Не удалось" in send.call_args.args[1]


def test_second_tap_carrying_a_stale_keyboard_keeps_the_first_tick(store, fake_pool):
    markup = picker(store, fake_pool)
    first = press("pln:t:110:1", markup, message_id=6000)
    run(notify.handle_callback(first))

    second = press("pln:t:110:2", markup, message_id=6000)
    run(notify.handle_callback(second))

    assert labels(redrawn(second))[:2] == ["✅ @ann", "✅ Bob"]


def test_send_for_a_missing_playlist_warns_and_can_be_retried(store, fake_pool):
    add_people(store)
    markup = offer(fake_pool).call_args.kwargs["reply_markup"]
    tick = press("pln:t:110:2", markup, message_id=7000)
    run(notify.handle_callback(tick))

    missing = press("pln:send:110", redrawn(tick), message_id=7000)
    send, _ = tap(missing, fake_pool)

    send.assert_not_called()
    assert missing.answer.call_args.kwargs.get("show_alert") is True

    store.add_playlist(id=1, spotify_id="p", name=PLAYLIST["name"], number=110, url=PLAYLIST["url"])
    send, _ = tap(press("pln:send:110", redrawn(tick), message_id=7000), fake_pool)
    assert send.call_count == 1


def test_person_without_a_name_is_still_identifiable(store, fake_pool):
    store.add_user(telegram_id=77, spotify_id="x", telegram_name="")

    markup = offer(fake_pool).call_args.kwargs["reply_markup"]

    assert labels(markup)[0] == "⬜ id 77"


def test_malformed_tap_is_answered_and_changes_nothing(store, fake_pool):
    callback = press("pln:t:abc:1", picker(store, fake_pool))

    run(notify.handle_callback(callback))

    callback.message.edit_reply_markup.assert_not_called()
    callback.answer.assert_awaited_once()


def test_created_message_escapes_the_theme():
    from app.services import admin_commands

    created = {"name": "TURDOM#110 - R&B <3", "number": 110, "url": "u", "auto_closed": []}
    with patch("app.services.admin_commands.create_next_playlist", AsyncMock(return_value=created)), \
            patch("app.services.admin_commands.log_action", AsyncMock()):
        result = run(admin_commands.cmd_create_next(MagicMock(), theme="R&B <3"))

    assert "R&amp;B &lt;3" in result["message"]


def test_theme_reply_escapes_the_playlist_name():
    import app.bot as bot_package

    message = MagicMock()
    message.text = "R&B <3"
    message.from_user.id = ADMIN
    created = {**PLAYLIST, "name": "TURDOM#110 - R&B <3", "auto_closed": []}
    reply = AsyncMock()

    with patch("app.bot.session") as session, \
            patch("app.services.playlists.create_next_playlist", AsyncMock(return_value=created)), \
            patch("app.bot.core.get_pool"), \
            patch("app.bot.reply", reply), \
            patch("app.bot.offer_notification", AsyncMock()):
        session.waiting_theme = True
        run(bot_package.on_theme_input(message))

    assert "R&amp;B &lt;3" in reply.call_args.args[1]


def test_delivery_survives_a_failed_button_answer(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 2)
    callback = press("pln:send:110", markup)
    callback.answer.side_effect = Exception("query is too old")

    send, _ = tap(callback, fake_pool)

    assert send.call_count == 1


def test_report_is_sent_as_a_message_if_the_picker_cannot_be_edited(store, fake_pool):
    markup = ticked_markup(store, fake_pool, 2)
    send = AsyncMock()
    edit = AsyncMock(side_effect=Exception("telegram is down"))

    with patch("app.bot.playlist_notify.get_pool", return_value=fake_pool), \
            patch("app.bot.playlist_notify.send", send), \
            patch("app.bot.playlist_notify.edit_text", edit):
        run(notify.handle_callback(press("pln:send:110", markup)))

    assert [call.args[0] for call in send.call_args_list] == [2, ADMIN]
    assert "Получили: Bob" in send.call_args.args[1]


def test_failed_redraw_is_answered_and_does_not_keep_the_invisible_tick(store, fake_pool):
    markup = picker(store, fake_pool)
    failed = press("pln:t:110:1", markup, message_id=8000)
    failed.message.edit_reply_markup.side_effect = Exception("network timeout")
    run(notify.handle_callback(failed))
    failed.answer.assert_awaited_once()

    next_tap = press("pln:t:110:2", markup, message_id=8000)
    run(notify.handle_callback(next_tap))

    assert labels(redrawn(next_tap))[:2] == ["⬜ @ann", "✅ Bob"]


def test_picker_passes_its_keyboard_through_the_real_send_helper(store, fake_pool):
    from app.bot import core

    add_people(store)
    send_message = AsyncMock()

    with patch("app.bot.playlist_notify.get_pool", return_value=fake_pool), \
            patch("app.bot.playlist_notify.settings") as settings, \
            patch.object(core.bot, "send_message", send_message):
        settings.telegram_admin_id = ADMIN
        run(notify.offer_notification(PLAYLIST))

    assert labels(send_message.call_args.kwargs["reply_markup"])[0] == "⬜ @ann"


def test_clear_all_unticks_everyone(store, fake_pool):
    first = press("pln:all:110", picker(store, fake_pool))
    run(notify.handle_callback(first))
    second = press("pln:all:110", redrawn(first))

    run(notify.handle_callback(second))

    assert labels(redrawn(second)) == [
        "⬜ @ann", "⬜ Bob", "Выбрать всех", "📣 Отправить (0)", "Не отправлять",
    ]
