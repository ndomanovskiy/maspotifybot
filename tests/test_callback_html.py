"""Buttons that edit their own message keep its formatting and never let user text turn into markup."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message

from tests.conftest import telegram_message

ADMIN = 100


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def press(data, from_id=ADMIN, full_name="Admin", username="admin"):
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = from_id
    callback.from_user.full_name = full_name
    callback.from_user.username = username
    callback.answer = AsyncMock()
    callback.message = telegram_message("<Vasya> wants in", "wants")
    return callback


def edit_after(handler, callback, **patches):
    """Run the handler; return the edit_text mock and the send mock."""
    with ExitStack() as stack:
        edit = stack.enter_context(patch.object(Message, "edit_text", AsyncMock()))
        send = stack.enter_context(patch("app.bot.callbacks.send", AsyncMock()))
        stack.enter_context(patch("app.bot.core.settings")).telegram_admin_id = ADMIN
        stack.enter_context(patch("app.bot.callbacks.settings")).telegram_admin_id = ADMIN
        for name, value in patches.items():
            stack.enter_context(patch(f"app.bot.callbacks.{name}", value))
        run(handler(callback))
    return edit, send


def assert_formatted_without_preview(edit):
    assert edit.call_args.args[0].startswith("&lt;Vasya&gt; <b>wants</b> in")
    assert edit.call_args.kwargs["link_preview_options"].is_disabled is True


def test_deny_keeps_formatting_and_escapes_text():
    from app.bot import callbacks

    edit, _ = edit_after(callbacks.on_deny, press("deny:7"))

    assert_formatted_without_preview(edit)


def test_approve_keeps_formatting_and_escapes_text(fake_pool):
    from app.bot import callbacks

    session = MagicMock(participants=set(), active_session_id=None, update_session_message=AsyncMock())
    edit, _ = edit_after(callbacks.on_approve, press("approve:7"),
                         get_pool=MagicMock(return_value=fake_pool), session=session)

    assert_formatted_without_preview(edit)


def test_join_request_escapes_the_name_and_keeps_formatting():
    from app.bot import callbacks

    session = MagicMock(participants=set(), active_session_id=1)
    callback = press("join_session", from_id=7, full_name="<i>Bob</i>", username="b<o>b")

    edit, send = edit_after(callbacks.on_join_session, callback, session=session)

    assert_formatted_without_preview(edit)
    request = send.call_args.args[1]
    assert "&lt;i&gt;Bob&lt;/i&gt;" in request and "@b&lt;o&gt;b" in request


def test_join_command_escapes_the_name():
    from app.bot.commands import user

    message = MagicMock()
    message.from_user.id = 7
    message.from_user.full_name = "<i>Bob</i>"
    message.from_user.username = "b<o>b"
    message.answer = AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.commands.user.settings") as user_settings, \
            patch("app.bot.core.is_registered", AsyncMock(return_value=True)), \
            patch("app.bot.commands.user.session", MagicMock(participants=set(), active_session_id=1)), \
            patch("app.bot.commands.user.send", AsyncMock()) as send:
        settings.telegram_admin_id = ADMIN
        user_settings.telegram_admin_id = ADMIN
        run(user.cmd_join(message))

    request = send.call_args.args[1]
    assert "&lt;i&gt;Bob&lt;/i&gt;" in request and "@b&lt;o&gt;b" in request
