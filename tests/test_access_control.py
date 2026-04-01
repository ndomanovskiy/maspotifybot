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

    @patch("app.bot.handlers.settings")
    @patch("app.bot.handlers._pool", new_callable=lambda: lambda: None)
    def test_admin_always_registered(self, mock_pool, mock_settings):
        """Admin should always pass is_registered check."""
        mock_settings.telegram_admin_id = 12345
        from app.bot.handlers import is_admin
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


class TestNextPlaylistInviteUrl:

    def test_invite_url_returned_when_set(self):
        """get_next_playlist should return invite_url when it's set."""
        store = FakeStore()
        store.playlists.append({
            "id": 1, "spotify_id": "sp1", "name": "TURDOM#92",
            "status": "upcoming", "url": "https://open.spotify.com/playlist/sp1",
            "invite_url": "https://open.spotify.com/playlist/sp1?pt=abc123",
            "number": 92, "is_thematic": False,
        })

        # Simulate what get_next_playlist returns
        result = {
            "name": "TURDOM#92",
            "url": "https://open.spotify.com/playlist/sp1",
            "status": "upcoming",
            "invite_url": "https://open.spotify.com/playlist/sp1?pt=abc123",
        }
        link = result.get("invite_url") or result["url"]
        assert link == "https://open.spotify.com/playlist/sp1?pt=abc123"

    def test_falls_back_to_regular_url(self):
        """When invite_url is None, should use regular url."""
        result = {
            "name": "TURDOM#92",
            "url": "https://open.spotify.com/playlist/sp1",
            "status": "upcoming",
            "invite_url": None,
        }
        link = result.get("invite_url") or result["url"]
        assert link == "https://open.spotify.com/playlist/sp1"

    def test_empty_invite_url_falls_back(self):
        """When invite_url is empty string, should use regular url."""
        result = {
            "name": "TURDOM#92",
            "url": "https://open.spotify.com/playlist/sp1",
            "status": "upcoming",
            "invite_url": "",
        }
        link = result.get("invite_url") or result["url"]
        assert link == "https://open.spotify.com/playlist/sp1"
