"""Tests for Last.fm genre resolution and the 3-tier genre chain."""

from unittest.mock import AsyncMock, patch, MagicMock
import asyncio

from app.services.genre_distributor import classify_track


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# Last.fm tag → GENRE_MAP classification
# ============================================================

class TestLastfmTagClassification:
    """Last.fm returns tags — verify they map through GENRE_MAP correctly."""

    def test_rock_tag(self):
        assert classify_track("rock") is not None

    def test_electronic_tag(self):
        assert classify_track("electronic") is not None

    def test_metal_tag(self):
        assert classify_track("metal") is not None

    def test_hip_hop_tag(self):
        assert classify_track("hip hop") is not None

    def test_trip_hop_to_chill(self):
        assert classify_track("trip hop") == "TURDOM Chill"

    def test_drum_and_bass(self):
        assert classify_track("drum and bass") == "TURDOM DnB"

    def test_unknown_tag(self):
        assert classify_track("zambian highlife") is None

    def test_compound_lastfm_tags(self):
        """Last.fm often returns compound tags like 'progressive rock'."""
        assert classify_track("progressive rock") == "TURDOM Rock"

    def test_alternative_metal(self):
        assert classify_track("alternative metal") == "TURDOM Metal"

    def test_future_bass(self):
        assert classify_track("future bass") == "TURDOM DnB"

    def test_neo_soul(self):
        assert classify_track("neo soul") == "TURDOM R&B"


# ============================================================
# Last.fm API client
# ============================================================

class TestLastfmClient:

    @patch("app.services.lastfm.settings")
    def test_no_api_key_returns_empty(self, mock_settings):
        """Without API key, should return empty list immediately."""
        mock_settings.lastfm_api_key = ""
        from app.services.lastfm import get_track_tags
        result = run(get_track_tags("Song", "Artist"))
        assert result == []

    @patch("app.services.lastfm._get_client")
    @patch("app.services.lastfm.settings")
    def test_successful_response(self, mock_settings, mock_get_client):
        """Valid response with tags should return tag names."""
        mock_settings.lastfm_api_key = "test_key"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "toptags": {
                "tag": [
                    {"name": "rock", "count": "100"},
                    {"name": "classic rock", "count": "80"},
                    {"name": "hard rock", "count": "50"},
                    {"name": "80s", "count": "10"},  # below threshold
                ]
            }
        }

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_get_client.return_value = mock_client

        from app.services.lastfm import get_track_tags
        result = run(get_track_tags("Song", "Artist"))
        assert "rock" in result
        assert "classic rock" in result
        assert "80s" not in result  # count < 20

    @patch("app.services.lastfm._get_client")
    @patch("app.services.lastfm.settings")
    def test_empty_response(self, mock_settings, mock_get_client):
        """Track not found — empty tags."""
        mock_settings.lastfm_api_key = "test_key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"toptags": {"tag": []}}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_get_client.return_value = mock_client

        from app.services.lastfm import get_track_tags
        result = run(get_track_tags("Unknown Song", "Unknown Artist"))
        assert result == []


# ============================================================
# Genre resolution chain
# ============================================================

class TestGenreResolutionChain:

    @patch("app.services.genre_resolver._resolve_spotify", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_ai", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_lastfm", new_callable=AsyncMock, return_value="rock")
    def test_lastfm_wins(self, mock_lastfm, mock_ai, mock_spotify):
        """Last.fm result should be used, AI and Spotify not called."""
        from app.services.genre_resolver import resolve_genre

        track = MagicMock()
        track.name = "Song"
        artist_mock = MagicMock()
        artist_mock.name = "Artist"
        track.artists = [artist_mock]

        result = run(resolve_genre(track))
        assert result == "rock"
        mock_ai.assert_not_called()
        mock_spotify.assert_not_called()

    @patch("app.services.genre_resolver._resolve_spotify", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_ai", new_callable=AsyncMock, return_value="metal")
    @patch("app.services.genre_resolver._resolve_lastfm", new_callable=AsyncMock, return_value=None)
    def test_ai_fallback(self, mock_lastfm, mock_ai, mock_spotify):
        """If Last.fm fails, AI should be used."""
        from app.services.genre_resolver import resolve_genre

        track = MagicMock()
        track.name = "Song"
        artist_mock = MagicMock()
        artist_mock.name = "Artist"
        track.artists = [artist_mock]

        result = run(resolve_genre(track))
        assert result == "metal"
        mock_spotify.assert_not_called()

    @patch("app.services.genre_resolver._resolve_spotify", new_callable=AsyncMock, return_value="indie rock, alternative")
    @patch("app.services.genre_resolver._resolve_ai", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_lastfm", new_callable=AsyncMock, return_value=None)
    def test_spotify_last_resort(self, mock_lastfm, mock_ai, mock_spotify):
        """If Last.fm and AI both fail, Spotify artist genres used."""
        from app.services.genre_resolver import resolve_genre

        track = MagicMock()
        track.name = "Song"
        artist_mock = MagicMock()
        artist_mock.name = "Artist"
        track.artists = [artist_mock]

        result = run(resolve_genre(track))
        assert result == "indie rock, alternative"

    @patch("app.services.genre_resolver._resolve_spotify", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_ai", new_callable=AsyncMock, return_value=None)
    @patch("app.services.genre_resolver._resolve_lastfm", new_callable=AsyncMock, return_value=None)
    def test_all_fail_returns_unknown(self, mock_lastfm, mock_ai, mock_spotify):
        """If everything fails, return 'unknown'."""
        from app.services.genre_resolver import resolve_genre

        track = MagicMock()
        track.name = "Song"
        artist_mock = MagicMock()
        artist_mock.name = "Artist"
        track.artists = [artist_mock]

        result = run(resolve_genre(track))
        assert result == "unknown"
