"""The session message lists participants by their Telegram names, which anyone can set to anything."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_participant_names_are_escaped_for_the_session_message():
    from app.bot.session_manager import session

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"telegram_username": None, "telegram_name": "<Bob> & Co"})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(session, "participants", {7}), patch("app.bot.core.get_pool", return_value=pool):
        names = run(session.get_participant_names())

    assert names == "&lt;Bob&gt; &amp; Co"
