"""/secret shows the participant's current secret as text, never as markup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_current_secret_is_escaped():
    from app.bot.commands import user
    from app.bot.session_manager import session

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 1})
    conn.fetchval = AsyncMock(return_value="мои треки <3")
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    message = MagicMock()
    message.text = "/secret"
    message.from_user.id = 7

    with patch.object(session, "active_session_id", 1), \
            patch("app.bot.core.is_registered", AsyncMock(return_value=True)), \
            patch("app.bot.commands.user.get_pool", return_value=pool), \
            patch("app.bot.commands.user.reply", AsyncMock()) as reply:
        run(user.cmd_secret(message))

    assert "<i>мои треки &lt;3</i>" in reply.call_args.args[1]
