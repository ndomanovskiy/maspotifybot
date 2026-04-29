"""Tests for classify_duplicates — phantom vs keep for listened playlists."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.playlists import classify_duplicates


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_pool(fetchrow_side_effect, fetchval_side_effect=None):
    """Build a fake asyncpg pool that returns predefined results."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


class TestClassifyDuplicatesPhantom:
    """Phantom filter must respect playlist status."""

    def test_listened_playlist_returns_keep_not_phantom(self):
        """Track in a 'listened' playlist with no session → keep (not phantom)."""
        duplicates = [
            {"playlist_id": 10, "spotify_track_id": "abc123", "match": "exact"},
        ]

        # fetchrow returns None (no session_tracks entry)
        # fetchval returns "listened" (playlist status)
        pool = _make_pool(
            fetchrow_side_effect=[None],
            fetchval_side_effect=["listened"],
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "keep"

    def test_active_playlist_returns_phantom(self):
        """Track in an 'active' playlist with no session → phantom."""
        duplicates = [
            {"playlist_id": 20, "spotify_track_id": "def456", "match": "exact"},
        ]

        pool = _make_pool(
            fetchrow_side_effect=[None],
            fetchval_side_effect=["active"],
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "phantom"

    def test_upcoming_playlist_returns_phantom(self):
        """Track in an 'upcoming' playlist with no session → phantom."""
        duplicates = [
            {"playlist_id": 30, "spotify_track_id": "ghi789", "match": "fuzzy_exact"},
        ]

        pool = _make_pool(
            fetchrow_side_effect=[None],
            fetchval_side_effect=["upcoming"],
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "phantom"

    def test_track_with_session_keeps_vote_result(self):
        """Track that was in a session uses its vote_result regardless of playlist status."""
        duplicates = [
            {"playlist_id": 10, "spotify_track_id": "abc123", "match": "exact"},
        ]

        # fetchrow returns a vote_result — fetchval should NOT be called
        fake_row = {"vote_result": "drop"}
        pool = _make_pool(
            fetchrow_side_effect=[fake_row],
            fetchval_side_effect=None,
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "drop"

    def test_multiple_duplicates_mixed_statuses(self):
        """Multiple duplicates: one from listened (keep), one from active (phantom)."""
        duplicates = [
            {"playlist_id": 10, "spotify_track_id": "abc", "match": "exact"},
            {"playlist_id": 20, "spotify_track_id": "def", "match": "fuzzy_exact"},
        ]

        pool = _make_pool(
            fetchrow_side_effect=[None, None],
            fetchval_side_effect=["listened", "active"],
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "keep"
        assert result[1]["session_status"] == "phantom"

    def test_empty_duplicates_returns_empty(self):
        """Empty list in → empty list out."""
        pool = MagicMock()
        result = run(classify_duplicates(pool, []))
        assert result == []

    def test_isrc_match_fallback_then_listened(self):
        """ISRC match: both fetchrow calls return None, playlist is listened → keep."""
        duplicates = [
            {"playlist_id": 10, "spotify_track_id": "abc", "match": "isrc"},
        ]

        # First fetchrow (by track_id) → None, second (by ISRC) → None
        pool = _make_pool(
            fetchrow_side_effect=[None, None],
            fetchval_side_effect=["listened"],
        )

        result = run(classify_duplicates(pool, duplicates))
        assert result[0]["session_status"] == "keep"
