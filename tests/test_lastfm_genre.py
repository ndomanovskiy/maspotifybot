"""Tests for Last.fm genre tags and the genre resolution chain (track tags, artist tags, AI)."""

from unittest.mock import AsyncMock, patch, MagicMock
import asyncio
import logging

import anthropic
import httpx
import pytest

from app.services.genre_distributor import classify_track, classify_track_multi


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

    @patch("app.services.lastfm._get_client")
    @patch("app.services.lastfm.settings")
    def test_artist_tags_keep_only_strong_ones(self, mock_settings, mock_get_client):
        """Artist tags come from artist.getTopTags, weak ones dropped as for a track."""
        mock_settings.lastfm_api_key = "test_key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"toptags": {"tag": [
            {"name": "Hip Hop", "count": "100"},
            {"name": "pop punk", "count": "16"},
        ]}}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_get_client.return_value = mock_client

        from app.services.lastfm import get_artist_tags
        result = run(get_artist_tags("ZEP"))

        assert result == ["hip hop"]
        params = mock_client.get.call_args.kwargs["params"]
        assert (params["method"], params["artist"]) == ("artist.getTopTags", "ZEP")


def lastfm_answering(json_body=None, error=None):
    """Patch the Last.fm client to answer with this JSON, or to fail with this exception."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = json_body
    client = AsyncMock()
    if error:
        client.get.side_effect = error
    else:
        client.get.return_value = response
    settings = patch("app.services.lastfm.settings", lastfm_api_key="SECRET123")
    return settings, patch("app.services.lastfm._get_client", return_value=client)


def ask_lastfm(coro_factory, json_body=None, error=None):
    settings, client = lastfm_answering(json_body, error)
    with settings, client:
        return run(coro_factory())


class TestLastfmAnswers:
    """Last.fm that knows nothing ([]) is told apart from Last.fm that did not answer (None)."""

    def test_no_connection_is_no_answer(self):
        from app.services.lastfm import get_artist_tags

        assert ask_lastfm(lambda: get_artist_tags("ZEP"), error=httpx.ConnectError("proxy is down")) is None

    def test_unknown_artist_is_an_answer_without_tags(self):
        from app.services.lastfm import get_artist_tags
        not_found = {"error": 6, "message": "The artist you supplied could not be found"}

        assert ask_lastfm(lambda: get_artist_tags("Nobody"), not_found) == []

    def test_service_error_is_no_answer(self):
        from app.services.lastfm import get_track_tags

        assert ask_lastfm(lambda: get_track_tags("Song", "X"), {"error": 11, "message": "Service Offline"}) is None

    def test_http_error_is_no_answer_and_keeps_the_key_out_of_the_log(self, caplog):
        from app.services.lastfm import get_track_tags
        request = httpx.Request("GET", "https://ws.audioscrobbler.com/2.0/?api_key=SECRET123")
        forbidden = httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url '{request.url}'",
            request=request, response=httpx.Response(403, request=request),
        )

        with caplog.at_level(logging.DEBUG):
            assert ask_lastfm(lambda: get_track_tags("Song", "X"), error=forbidden) is None

        assert "403" in caplog.text
        assert "SECRET123" not in caplog.text

    def test_a_request_last_fm_calls_invalid_is_an_answer_without_tags(self):
        """Last.fm answers 400 to a track with an empty artist: that is not Last.fm being down."""
        from app.services.lastfm import get_track_tags
        request = httpx.Request("GET", "https://ws.audioscrobbler.com/2.0/")
        invalid = httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))

        assert ask_lastfm(lambda: get_track_tags("Song", ""), error=invalid) == []

    def test_a_refused_key_is_as_if_there_were_no_lastfm(self):
        """An invalid or suspended key lasts until someone fixes it: the chain goes on without Last.fm."""
        from app.services.lastfm import get_artist_tags
        request = httpx.Request("GET", "https://ws.audioscrobbler.com/2.0/")
        refused = httpx.HTTPStatusError("403", request=request, response=httpx.Response(
            403, request=request, json={"error": 10, "message": "Invalid API key - You must be granted a valid key"},
        ))

        assert ask_lastfm(lambda: get_artist_tags("ZEP"), error=refused) == []

    def test_failed_authentication_is_as_if_there_were_no_lastfm(self):
        from app.services.lastfm import get_artist_tags

        assert ask_lastfm(lambda: get_artist_tags("ZEP"), {"error": 4, "message": "Authentication Failed"}) == []

    def test_a_rate_limit_is_no_answer(self):
        from app.services.lastfm import get_artist_tags
        request = httpx.Request("GET", "https://ws.audioscrobbler.com/2.0/")
        limited = httpx.HTTPStatusError("429", request=request, response=httpx.Response(
            429, request=request, json={"error": 29, "message": "Rate limit exceeded"},
        ))

        assert ask_lastfm(lambda: get_artist_tags("ZEP"), error=limited) is None

    def test_a_lone_tag_given_as_an_object_still_counts(self):
        from app.services.lastfm import get_track_tags
        one_tag = {"toptags": {"tag": {"name": "Rock", "count": "100"}}}

        assert ask_lastfm(lambda: get_track_tags("Song", "X"), one_tag) == ["rock"]


# ============================================================
# Genre resolution chain
# ============================================================

CHAIN = "app.services.genre_resolver"


def track_by(*artist_names):
    track = MagicMock()
    track.name = "Song"
    track.artists = []
    for name in artist_names:
        artist = MagicMock()
        artist.name = name
        track.artists.append(artist)
    return track


def claude_answering(text=None, error=None):
    """An Anthropic client whose reply says `text`, or fails with `error`."""
    client = MagicMock()
    reply = MagicMock()
    reply.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=reply, side_effect=error)
    return client


def anthropic_error(kind, status, message):
    """An Anthropic API error the way the SDK raises it."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return kind(f"Error code: {status} - {body}", response=httpx.Response(status, request=request), body=body)


class Sources:
    """Last.fm and Claude answering as a test says. None stands for Last.fm that did not answer."""

    def __init__(self, track_tags=(), artists=((),), ai=None, ai_error=None):
        self.track_tags = AsyncMock(return_value=None if track_tags is None else list(track_tags))
        self.artist_tags = AsyncMock(side_effect=[None if tags is None else list(tags) for tags in artists])
        self.claude = claude_answering(ai or "unknown", ai_error)

    def resolve(self, track=None):
        from app.services.genre_resolver import resolve_genre
        with patch(f"{CHAIN}.get_track_tags", self.track_tags), \
                patch(f"{CHAIN}.get_artist_tags", self.artist_tags), \
                patch("app.services.ai.settings", anthropic_api_key="key"), \
                patch("app.services.ai._get_anthropic", return_value=self.claude):
            return run(resolve_genre(track or track_by("Artist")))

    @property
    def ai_asked(self):
        return self.claude.messages.create.await_count > 0


class TestGenreResolutionChain:

    def test_track_tags_win(self):
        sources = Sources(track_tags=["rock", "80s"])

        assert sources.resolve() == "rock"
        sources.artist_tags.assert_not_called()
        assert not sources.ai_asked

    def test_artist_tags_when_the_track_has_none(self):
        """No track tags: the first artist with genre tags decides, and AI is not asked."""
        sources = Sources(artists=[["russian"], ["rap", "hip hop", "experimental"]])

        assert sources.resolve(track_by("Unknown", "Feat")) == "rap, hip hop"
        assert [c.args[0] for c in sources.artist_tags.call_args_list] == ["Unknown", "Feat"]
        assert not sources.ai_asked

    def test_only_the_first_three_artists_are_looked_up(self):
        sources = Sources(artists=[[]] * 5)

        sources.resolve(track_by("A", "B", "C", "D", "E"))

        assert sources.artist_tags.await_count == 3

    def test_ai_when_lastfm_knows_nothing(self):
        assert Sources(ai="Metal").resolve() == "metal"

    def test_ai_names_become_tags_the_distribution_knows(self):
        """Claude answers with playlist names; 'Hip-Hop' and 'DnB' alone would match no playlist."""
        genre = Sources(ai="Hip-Hop, DnB").resolve()

        assert genre == "hip hop, drum and bass"
        assert classify_track_multi(genre) == ["TURDOM DnB", "TURDOM Hip-Hop"]

    def test_nothing_anywhere_is_none(self):
        assert Sources(ai="unknown").resolve() is None

    def test_lastfm_not_answering_skips_the_artists_and_still_asks_ai(self):
        sources = Sources(track_tags=None, ai="Pop")

        assert sources.resolve() == "pop"
        sources.artist_tags.assert_not_called()

    def test_lastfm_not_answering_and_ai_knowing_nothing_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed

        with pytest.raises(GenreLookupFailed):
            Sources(track_tags=None, ai="unknown").resolve()

    def test_ai_not_answering_with_nothing_found_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        overloaded = anthropic_error(anthropic.RateLimitError, 429, "Number of requests has exceeded your rate limit")

        with pytest.raises(GenreLookupFailed):
            Sources(ai_error=overloaded).resolve()

    def test_ai_without_credit_is_as_if_there_were_no_ai(self):
        """An empty credit balance lasts until someone pays: the person who added the track is asked."""
        no_credit = anthropic_error(
            anthropic.BadRequestError, 400,
            "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.",
        )

        assert Sources(ai_error=no_credit).resolve() is None

    def test_ai_rejecting_the_key_is_as_if_there_were_no_ai(self):
        rejected = anthropic_error(anthropic.AuthenticationError, 401, "invalid x-api-key")

        assert Sources(ai_error=rejected).resolve() is None

    def test_ai_with_a_retired_model_is_as_if_there_were_no_ai(self):
        retired = anthropic_error(anthropic.NotFoundError, 404, "model: claude-sonnet-4-6")

        assert Sources(ai_error=retired).resolve() is None

    def test_ai_past_its_spending_limit_is_as_if_there_were_no_ai(self):
        limit = anthropic_error(anthropic.BadRequestError, 400, "You have reached your specified API usage limits.")

        assert Sources(ai_error=limit).resolve() is None

    def test_no_ai_key_means_no_ai(self):
        sources = Sources()

        with patch("app.services.ai.settings", anthropic_api_key=""), \
                patch("app.services.ai._get_anthropic", return_value=sources.claude):
            from app.services.genre_resolver import _resolve_ai
            assert run(_resolve_ai("Song", "Artist")) is None
        assert not sources.ai_asked

    def test_ai_is_asked_with_a_short_timeout_and_only_what_the_sdk_takes(self):
        """The anthropic SDK in the bot's image (2.7) has no `temperature` parameter at all."""
        sources = Sources(ai="Rock")

        sources.resolve()

        kwargs = sources.claude.messages.create.call_args.kwargs
        assert kwargs["timeout"] == 30
        assert "temperature" not in kwargs

    def test_any_other_ai_error_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        bad_request = anthropic_error(anthropic.BadRequestError, 400, "max_tokens: field required")

        with pytest.raises(GenreLookupFailed):
            Sources(ai_error=bad_request).resolve()

    def test_lastfm_stopping_on_an_artist_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        sources = Sources(artists=[None, ["rock"]])

        with pytest.raises(GenreLookupFailed):
            sources.resolve(track_by("A", "B"))
        assert sources.artist_tags.await_count == 1


