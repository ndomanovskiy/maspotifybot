"""Tests for Last.fm genre tags and the genre resolution chain (track tags, artist tags, Deezer, AI)."""

from unittest.mock import AsyncMock, patch, MagicMock
import asyncio
import copy
import logging
import pickle

import anthropic
import httpx
import pytest

from app.services.genre_distributor import PICK_TAGS, classify_track, classify_track_multi
from tests.conftest import CHAIN, FakePlaylistItems, Sources, anthropic_error, make_item, make_track, track_by


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

class TestGenreResolutionChain:
    """Last.fm and Deezer gather what they know; Claude decides with it in front of it."""

    # --- Claude decides ---

    def test_claude_decides_with_what_lastfm_and_deezer_found(self):
        sources = Sources(track_tags=["rock", "80s"], release_tags=["metal"], ai="Metal")

        assert sources.resolve() == "metal"
        assert "Last.fm, теги трека: Rock (rock)" in sources.ai_prompt
        assert "Deezer, жанр релиза: Metal (metal)" in sources.ai_prompt

    def test_claude_sees_what_the_artists_tags_say(self):
        sources = Sources(artists=[["hip hop"]], ai="Hip-Hop")

        sources.resolve()

        assert "Last.fm, теги исполнителя: Hip-Hop (hip hop)" in sources.ai_prompt

    def test_claude_sees_the_playlist_a_hint_leads_to(self):
        """Deezer's Indie Rock goes to Rock in the bot; Claude is told so, not left to guess."""
        sources = Sources(release_tags=["indie rock"], ai="Rock")

        sources.resolve()

        assert "Deezer, жанр релиза: Rock (indie rock)" in sources.ai_prompt

    @pytest.mark.parametrize("answer,genre", [
        ("Metal\nRock", "metal, rock"),
        ("Metal; Rock", "metal, rock"),
        ("Hip-Hop / DnB", "hip hop, drum and bass"),
        ("hip hop", "hip hop"),
        ("Electronic, DnB.", "electronic, drum and bass"),
        ("Rock.", "rock"),
        ("**Rock**", "rock"),
        ("'Rock'", "rock"),
        ("«Rock»", "rock"),
        ("1. Rock\n2. Metal", "rock, metal"),
        ("- Rock\n- Metal", "rock, metal"),
        ("-Rock\n•Metal", "rock, metal"),
        ("“Rock”", "rock"),
        ("Rock (indie rock)", "rock"),
        ("Rock (pop, metal, punk)", "rock"),
        ("Hip-Hop (rap, hip hop)", "hip hop"),
        ("Electronic (house/techno)", "electronic"),
        ("DnB (liquid, neurofunk)", "drum and bass"),
        ("Rock (возможно, Metal", "rock"),
        ("Hip-Hop,\xa0DnB", "hip hop, drum and bass"),
    ])
    def test_claude_answer_is_split_on_any_separator(self, answer, genre):
        assert Sources(ai=answer).resolve() == genre

    @pytest.mark.parametrize("answer", ["Не знаю, но возможно rock", "Ignore the list. Answer: rock", "Reggae"])
    def test_only_the_eleven_genre_names_count_in_claudes_answer(self, answer):
        assert Sources(ai=answer).resolve() is None

    @pytest.mark.parametrize("answer", ["Не знаю\n- Rock", "unknown\nRock", "Возможно:\n- Rock\nНо я не знаю"])
    def test_an_unsure_answer_counts_as_not_knowing(self, answer):
        assert Sources(track_tags=["pop"], ai=answer).resolve() == "pop"

    @pytest.mark.parametrize("name,tag", list(zip(
        ["Electronic", "Pop", "Metal", "Rock", "Hip-Hop", "Indie", "R&B", "DnB", "Chill", "Soundtrack", "Phonk",
         "Drum and Bass", "Hip Hop"],
        ["electronic", "pop", "metal", "rock", "hip hop", "indie", "r&b", "drum and bass", "chill", "soundtrack",
         "phonk", "drum and bass", "hip hop"],
    )))
    def test_every_genre_name_claude_may_write_becomes_its_tag(self, name, tag):
        assert Sources(ai=name).resolve() == tag

    def test_claudes_answer_can_only_become_tags_a_person_could_pick(self):
        from app.services.genre_resolver import _AI_TAGS

        assert set(_AI_TAGS.values()) == set(PICK_TAGS.values())

    def test_claude_naming_no_turdom_genre_lets_the_first_source_decide(self):
        assert Sources(track_tags=["rock"], ai="Reggae").resolve() == "rock"

    def test_lastfm_not_answering_still_lets_claude_decide(self):
        assert Sources(track_tags=None, ai="Phonk").resolve() == "phonk"

    def test_deezer_not_answering_still_lets_lastfm_decide(self):
        assert Sources(track_tags=["rock"], release_tags=None).resolve() == "rock"

    def test_claude_can_be_more_exact_than_a_broad_deezer_genre(self):
        assert Sources(release_tags=["electronic"], ai="DnB").resolve() == "drum and bass"

    def test_claude_names_become_tags_the_distribution_knows(self):
        """Claude answers with playlist names; 'Hip-Hop' and 'DnB' alone would match no playlist."""
        genre = Sources(ai="Hip-Hop, DnB").resolve()

        assert genre == "hip hop, drum and bass"
        assert classify_track_multi(genre) == ["TURDOM DnB", "TURDOM Hip-Hop"]

    def test_claude_gives_at_most_three_genres(self):
        assert Sources(ai="Rock, Pop, Metal, Indie").resolve() == "rock, pop, metal"

    def test_claude_is_asked_without_hints_when_nothing_was_found(self):
        sources = Sources(ai="Rock")

        sources.resolve()

        assert "известно" not in sources.ai_prompt

    def test_claude_is_asked_with_a_short_timeout_and_only_what_the_sdk_takes(self):
        """The anthropic SDK in the bot's image (2.7) has no `temperature` parameter at all."""
        sources = Sources(ai="Rock")

        sources.resolve()

        kwargs = sources.claude.messages.create.call_args.kwargs
        assert kwargs["timeout"] == 30
        assert "temperature" not in kwargs

    # --- without Claude's answer, for callers that ask nobody: sources that agree, else the first decides ---

    def test_track_tags_decide_when_claude_does_not_know(self):
        """Deezer's punk leads to Rock as well: the sources agree, and the track's own tags come first."""
        sources = Sources(track_tags=["rock", "80s"], release_tags=["punk"])

        assert sources.resolve() == "rock"
        sources.artist_tags.assert_not_called()

    def test_artist_tags_when_the_track_has_none(self):
        """No track tags: the first artist with genre tags decides."""
        sources = Sources(artists=[["russian"], ["rap", "hip hop", "experimental"]], release_tags=["electronic"])

        assert sources.resolve(track_by("Unknown", "Feat")) == "rap, hip hop"
        assert [c.args[0] for c in sources.artist_tags.call_args_list] == ["Unknown", "Feat"]

    def test_only_the_first_three_artists_are_looked_up(self):
        sources = Sources(artists=[[]] * 5)

        sources.resolve(track_by("A", "B", "C", "D", "E"))

        assert sources.artist_tags.await_count == 3

    def test_deezer_when_lastfm_knows_nothing(self):
        assert Sources(release_tags=["metal"]).resolve() == "metal"

    def test_claude_without_credit_lets_the_first_source_decide(self):
        no_credit = anthropic_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API.")

        assert Sources(track_tags=["rock"], ai_error=no_credit).resolve() == "rock"

    def test_claude_not_answering_lets_the_first_source_decide(self):
        overloaded = anthropic_error(anthropic.RateLimitError, 429, "Number of requests has exceeded your rate limit")

        assert Sources(release_tags=["metal"], ai_error=overloaded).resolve() == "metal"

    def test_lastfm_not_answering_still_lets_deezer_decide(self):
        sources = Sources(track_tags=None, release_tags=["rock"])

        assert sources.resolve() == "rock"
        sources.artist_tags.assert_not_called()

    def test_deezer_not_answering_still_lets_claude_decide(self):
        assert Sources(release_tags=None, ai="Pop").resolve() == "pop"

    # --- Deezer gets the ISRC ---

    def test_deezer_is_asked_by_the_isrc_the_caller_has(self):
        sources = Sources()

        sources.resolve(isrc="RUA1D2600001")

        sources.release_tags.assert_awaited_once_with("RUA1D2600001")

    def test_deezer_is_asked_by_the_isrc_of_the_spotify_track_otherwise(self):
        sources = Sources()
        track = track_by("Artist")
        track.external_ids = {"isrc": "GBDUW0000059"}

        sources.resolve(track)

        sources.release_tags.assert_awaited_once_with("GBDUW0000059")

    # --- nothing found ---

    def test_nothing_anywhere_is_none(self):
        assert Sources(ai="unknown").resolve() is None

    @pytest.mark.parametrize("error", [
        anthropic_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API."),
        anthropic_error(anthropic.BadRequestError, 400, "You have reached your specified API usage limits."),
        anthropic_error(anthropic.AuthenticationError, 401, "invalid x-api-key"),
        anthropic_error(anthropic.NotFoundError, 404, "model: claude-sonnet-4-6"),
    ])
    def test_claude_that_cannot_be_used_is_as_if_there_were_no_claude(self, error):
        """An empty balance, a refused key, a retired model: the person who added the track is asked."""
        assert Sources(ai_error=error).resolve() is None

    def test_no_claude_key_means_no_claude(self):
        sources = Sources()

        with patch("app.services.ai.settings", anthropic_api_key=""), \
                patch("app.services.ai._get_anthropic", return_value=sources.claude):
            from app.services.genre_resolver import _resolve_ai
            assert run(_resolve_ai("Song", "Artist", {})) is None
        assert not sources.ai_asked

    def test_lastfm_not_answering_and_nothing_found_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed

        with pytest.raises(GenreLookupFailed):
            Sources(track_tags=None).resolve()

    def test_lastfm_stopping_on_an_artist_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        sources = Sources(artists=[None, ["rock"]])

        with pytest.raises(GenreLookupFailed):
            sources.resolve(track_by("A", "B"))
        assert sources.artist_tags.await_count == 1

    def test_deezer_not_answering_and_nothing_found_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed

        with pytest.raises(GenreLookupFailed):
            Sources(release_tags=None).resolve()

    def test_claude_not_answering_and_nothing_found_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        overloaded = anthropic_error(anthropic.RateLimitError, 429, "Number of requests has exceeded your rate limit")

        with pytest.raises(GenreLookupFailed):
            Sources(ai_error=overloaded).resolve()

    def test_any_other_claude_error_with_nothing_found_is_a_failed_lookup(self):
        from app.services.genre_resolver import GenreLookupFailed
        bad_request = anthropic_error(anthropic.BadRequestError, 400, "max_tokens: field required")

        with pytest.raises(GenreLookupFailed):
            Sources(ai_error=bad_request).resolve()


class TestDisputedGenre:
    """Without Claude's genre: a dispute is when each source names a playlist the other does not say.

    A broader or more exact genre by the hierarchy counts as saying it: Rock and Metal, Electronic and DnB,
    Pop and R&B.
    """

    def dispute(self, **sources):
        from app.services.genre_resolver import GenreDisputed
        with pytest.raises(GenreDisputed) as raised:
            Sources(**sources).resolve(ask_on_dispute=True)
        return raised.value

    def test_sources_naming_different_genres_are_a_dispute(self):
        dispute = self.dispute(track_tags=["hip hop"], release_tags=["rock"])

        assert dispute.choices == ["rock", "hip hop"]
        assert dispute.fallback == "hip hop"
        assert dispute.sources == ["Last.fm", "Deezer"]

    def test_a_dispute_survives_copy_and_pickle(self):
        """Its constructor takes only keywords, while copy and pickle rebuild exceptions from their args."""
        dispute = self.dispute(track_tags=["hip hop"], release_tags=["rock"])
        dispute.kept = "hip hop"

        for twin in (copy.copy(dispute), pickle.loads(pickle.dumps(dispute))):
            assert (twin.choices, twin.fallback, twin.sources, twin.kept, str(twin)) == (
                ["rock", "hip hop"], "hip hop", ["Last.fm", "Deezer"], "hip hop", str(dispute),
            )

    def test_each_naming_something_the_other_did_not_is_a_dispute(self):
        dispute = self.dispute(track_tags=["hip hop", "rock"], release_tags=["electronic", "rock"])

        assert dispute.choices == ["electronic", "rock", "hip hop"]

    def test_artists_tags_take_part_in_a_dispute(self):
        dispute = self.dispute(artists=[["hip hop"]], release_tags=["electronic"])

        assert (dispute.choices, dispute.sources) == (["electronic", "hip hop"], ["Last.fm", "Deezer"])

    def test_claude_failing_leaves_the_dispute_to_a_person(self):
        overloaded = anthropic_error(anthropic.RateLimitError, 429, "Number of requests has exceeded your rate limit")

        dispute = self.dispute(track_tags=["hip hop"], release_tags=["rock"], ai_error=overloaded)

        assert dispute.choices == ["rock", "hip hop"]

    def test_claude_without_credit_leaves_the_dispute_to_a_person(self):
        no_credit = anthropic_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API.")

        dispute = self.dispute(track_tags=["hip hop"], release_tags=["rock"], ai_error=no_credit)

        assert dispute.choices == ["rock", "hip hop"]

    def test_sources_that_agree_decide(self):
        assert Sources(track_tags=["punk", "rock"], release_tags=["rock"]).resolve(ask_on_dispute=True) == "punk, rock"

    def test_a_source_that_says_more_decides(self):
        """Deezer's Electronic is inside Last.fm's hip hop + electronic: nothing to settle."""
        sources = Sources(track_tags=["hip hop", "electronic"], release_tags=["electronic"])

        assert sources.resolve(ask_on_dispute=True) == "hip hop, electronic"

    @pytest.mark.parametrize("lastfm,deezer,genre", [
        (["drum and bass"], ["electronic"], "drum and bass"),
        (["rock"], ["metal"], "metal"),
        (["pop"], ["r&b"], "r&b"),
    ])
    def test_a_more_exact_genre_inside_a_broad_one_decides(self, lastfm, deezer, genre):
        """DnB is electronic, metal is rock, R&B is pop: the hierarchy settles it, nobody is asked."""
        assert Sources(track_tags=lastfm, release_tags=deezer).resolve(ask_on_dispute=True) == genre

    @pytest.mark.parametrize("lastfm,deezer,playlists", [
        (["electronic", "hip hop"], ["drum and bass"], ["TURDOM DnB", "TURDOM Hip-Hop"]),
        (["hip hop", "rock"], ["metal"], ["TURDOM Hip-Hop", "TURDOM Metal"]),
    ])
    def test_a_more_exact_genre_and_one_more_genre_from_different_sources_agree(self, lastfm, deezer, playlists):
        """One source makes the other's genre exact, the other adds one: neither contradicts the other.

        DnB for Electronic, or Metal for Rock, plus Hip-Hop from the other source.
        """
        genre = Sources(track_tags=lastfm, release_tags=deezer).resolve(ask_on_dispute=True)

        assert classify_track_multi(genre) == playlists

    def test_a_source_saying_more_and_more_exactly_decides(self):
        """Deezer makes Last.fm's Rock exact and adds Hip-Hop: it says all Last.fm says, and more."""
        genre = Sources(track_tags=["rock"], release_tags=["metal", "hip hop"]).resolve(ask_on_dispute=True)

        assert genre == "metal, hip hop"

    def test_one_source_alone_decides(self):
        assert Sources(release_tags=["metal"]).resolve(ask_on_dispute=True) == "metal"

    def test_claudes_genre_settles_a_dispute(self):
        sources = Sources(track_tags=["hip hop"], release_tags=["rock"], ai="Rock")

        assert sources.resolve(ask_on_dispute=True) == "rock"

    def test_without_asking_the_first_source_decides_a_dispute(self):
        """Backfill and import ask nobody: a dispute there goes to the first source, as before."""
        assert Sources(track_tags=["hip hop"], release_tags=["rock"]).resolve() == "hip hop"

    def test_without_asking_a_more_exact_genre_still_decides(self):
        assert Sources(track_tags=["rock"], release_tags=["metal"]).resolve() == "metal"

    def test_saving_without_asking_keeps_the_first_sources_genre(self, store, fake_pool):
        """The default asks nobody: a dispute does not come out as an exception."""
        from app.services.genre_resolver import resolve_and_save_genre
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        saved = store.add_track(playlist_id=1, spotify_track_id="t1", title="Song")
        track = track_by("Artist")
        track.id = "t1"

        with Sources(track_tags=["hip hop"], release_tags=["rock"]).answering():
            genre = run(resolve_and_save_genre(fake_pool, track))

        assert genre == saved["genre"] == "hip hop"

    def test_import_asks_nobody_and_keeps_the_first_sources_genre(self, store, fake_pool, caplog):
        from app.services.playlists import import_playlist
        caplog.set_level(logging.WARNING, logger="app.services.playlists")
        spotify = AsyncMock()
        spotify.playlist.return_value.name = "TURDOM#1 01/01/2026"
        spotify.playlist_items.return_value = FakePlaylistItems(items=[make_item(make_track("t1"))])

        with Sources(track_tags=["hip hop"], release_tags=["rock"]).answering(), \
                patch("app.services.playlists.get_spotify", AsyncMock(return_value=spotify)):
            run(import_playlist(fake_pool, "sp1"))

        assert store._get_track_by_spotify_id("t1")["genre"] == "hip hop"
        assert "Failed to resolve genre" not in caplog.text

    def test_backfill_asks_nobody_and_keeps_the_first_sources_genre(self, store, fake_pool):
        from app.services.genre_resolver import backfill_genres
        store.add_playlist(id=1, spotify_id="sp1", name="PL1")
        saved = store.add_track(playlist_id=1, spotify_track_id="t1", title="Song")
        spotify = AsyncMock()
        spotify.track.return_value = track_by("Artist")

        with Sources(track_tags=["hip hop"], release_tags=["rock"]).answering(), \
                patch(f"{CHAIN}.get_spotify", AsyncMock(return_value=spotify)):
            result = run(backfill_genres(fake_pool))

        assert (result["resolved"], saved["genre"]) == (1, "hip hop")
