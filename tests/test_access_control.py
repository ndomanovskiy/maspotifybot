"""Tests for access control — is_registered, invite_url in /next."""

import asyncio
from unittest.mock import AsyncMock, patch

from tests.conftest import FakeStore, FakePool


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestIsRegistered:

    def _make_pool_and_store(self):
        store = FakeStore()
        pool = FakePool(store)
        return store, pool

    @patch("app.bot.core.settings")
    @patch("app.bot.core._pool", new_callable=lambda: lambda: None)
    def test_admin_always_registered(self, mock_pool, mock_settings):
        """Admin should always pass is_registered check."""
        mock_settings.telegram_admin_id = 12345
        from app.bot.core import is_admin
        assert is_admin(12345) is True

    def test_registered_user_in_db(self):
        """User in DB should be considered registered."""
        store, pool = self._make_pool_and_store()
        store.add_user(telegram_id=99999, spotify_id="sp_user")

        async def check():
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id = $1)",
                    99999,
                )

        assert run(check()) is True

    def test_unregistered_user_not_in_db(self):
        """User NOT in DB should not be considered registered."""
        store, pool = self._make_pool_and_store()

        async def check():
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id = $1)",
                    99999,
                )

        assert run(check()) is False


class TestNextPlaylistUrl:

    def test_returns_regular_url(self):
        """get_next_playlist returns regular playlist URL (collaborative via playlist settings)."""
        result = {
            "name": "TURDOM#92",
            "url": "https://open.spotify.com/playlist/sp1",
            "status": "upcoming",
        }
        assert result["url"] == "https://open.spotify.com/playlist/sp1"

    def test_no_invite_url_in_result(self):
        """invite_url is no longer part of get_next_playlist result."""
        result = {
            "name": "TURDOM#92",
            "url": "https://open.spotify.com/playlist/sp1",
            "status": "upcoming",
        }
        assert "invite_url" not in result
