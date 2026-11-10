"""«Перегенерировать факты» never leaves a track without the facts it had."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

TRACK = {"spotify_track_id": "t1", "title": "Song", "artist": "X", "album": "A"}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def regen(new_facts, callback=None):
    """Press the button for a track that has facts; return the executed SQL statements."""
    from app.bot import callbacks
    from app.bot.session_manager import session

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=TRACK)
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    callback = callback or MagicMock()
    callback.data = "regen_facts:5"
    callback.from_user.id = 100
    callback.answer = AsyncMock()
    callback.message.answer = AsyncMock()

    with patch("app.bot.core.settings") as settings, \
            patch("app.bot.callbacks.get_pool", return_value=pool), \
            patch("app.spotify.auth.get_spotify", AsyncMock(side_effect=Exception("offline"))), \
            patch("app.bot.callbacks.generate_track_facts", AsyncMock(return_value=new_facts)), \
            patch.object(session, "track_messages", {}), \
            patch("app.bot.callbacks.send", AsyncMock()):
        settings.telegram_admin_id = 100
        run(callbacks.on_regen_facts(callback))
    return [c.args[0] for c in conn.execute.await_args_list]


def test_failed_generation_keeps_the_old_facts():
    callback = MagicMock()

    assert regen("", callback) == []
    assert "ничего не изменил" in callback.message.answer.call_args.args[0]


def test_new_facts_replace_the_old_ones():
    statements = regen("🎤 new facts")

    assert len(statements) == 1 and "SET ai_facts = $1" in statements[0]
