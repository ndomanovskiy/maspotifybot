"""Deezer: the genre a distributor gave a track's release, found by ISRC."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.genre_distributor import classify_track_multi


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def answer(body):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = body
    return response


TRACK = {"id": 1, "artist": {"id": 27}, "album": {"id": 77}}
ISRC = "RUA1D2600001"
NO_DATA = {"error": {"type": "DataException", "message": "no data", "code": 800}}


def album(*genre_ids, artist_id=27, record_type="single"):
    return {
        "id": 77, "title": "Release", "record_type": record_type, "artist": {"id": artist_id},
        "genres": {"data": [{"id": genre_id, "name": "localized"} for genre_id in genre_ids]},
    }


def deezer_answering(*bodies, error=None):
    """Patch the Deezer client to answer the track and album requests in turn, or to fail."""
    client = AsyncMock()
    if error:
        client.get.side_effect = error
    else:
        client.get.side_effect = [body if isinstance(body, Exception) else answer(body) for body in bodies]
    return client


def tags_for(isrc, client):
    from app.services.deezer import get_release_genre_tags
    with patch("app.services.deezer._get_client", return_value=client):
        return run(get_release_genre_tags(isrc))


def test_the_release_genre_becomes_a_tag_the_playlists_know():
    client = deezer_answering(TRACK, album(464))

    assert tags_for("RUA1D2600001", client) == ["metal"]
    assert client.get.call_args_list[0].args[0].endswith("/track/isrc:RUA1D2600001")
    assert client.get.call_args_list[1].args[0].endswith("/album/77")


def test_the_isrc_is_written_the_way_deezer_finds_it():
    client = deezer_answering(TRACK, album(464))

    tags_for(" ru-a1d-26-00001", client)

    assert client.get.call_args_list[0].args[0].endswith("/track/isrc:RUA1D2600001")


@pytest.mark.parametrize("isrc", ["\tRUA1D2600001\n", "RUA1D/26?00#001"])
def test_stray_characters_never_reach_the_url(isrc):
    client = deezer_answering(TRACK, album(464))

    tags_for(isrc, client)

    assert client.get.call_args_list[0].args[0].endswith("/track/isrc:RUA1D2600001")


@pytest.mark.parametrize("isrc", ["ABC", "RUA1D26000011234"])
def test_something_that_is_no_isrc_asks_nothing(isrc):
    client = deezer_answering()

    assert tags_for(isrc, client) == []
    client.get.assert_not_called()


def test_a_release_whose_artists_deezer_does_not_name_decides_nothing():
    nameless_track = {"id": 1, "album": {"id": 77}}
    nameless_album = {"id": 77, "record_type": "single", "genres": {"data": [{"id": 152}]}}

    assert tags_for("X" * 12, deezer_answering(nameless_track, nameless_album)) == []


def test_alternative_alone_decides_nothing():
    assert tags_for(ISRC, deezer_answering(TRACK, album(85))) == []


def test_alternative_next_to_a_real_genre_gives_that_genre():
    assert tags_for(ISRC, deezer_answering(TRACK, album(85, 152))) == ["rock"]


def test_electro_and_dance_are_one_tag():
    assert tags_for(ISRC, deezer_answering(TRACK, album(106, 113))) == ["electronic"]


@pytest.mark.parametrize("genre_id", [85, 544, 144, 153, 95, 16])
def test_genres_with_no_turdom_playlist_decide_nothing(genre_id):
    assert tags_for(ISRC, deezer_answering(TRACK, album(genre_id))) == []


def test_someone_elses_release_says_nothing_about_the_track():
    """Deezer may give a soundtrack or a sampler of various artists for the ISRC."""
    soundtrack = album(173, artist_id=5080, record_type="album")

    assert tags_for(ISRC, deezer_answering(TRACK, soundtrack)) == []


def test_a_compilation_says_nothing_about_the_track():
    assert tags_for(ISRC, deezer_answering(TRACK, album(132, record_type="compile"))) == []


def test_an_unknown_track_is_an_answer_without_tags():
    assert tags_for(ISRC, deezer_answering(NO_DATA)) == []


def test_an_unknown_album_is_an_answer_without_tags():
    assert tags_for(ISRC, deezer_answering(TRACK, NO_DATA)) == []


def test_an_album_id_that_is_no_number_asks_nothing_more():
    client = deezer_answering({"id": 1, "artist": {"id": 27}, "album": {"id": "77/../user/1"}})

    assert tags_for(ISRC, client) == []
    assert client.get.await_count == 1


def test_a_genre_id_that_is_no_number_decides_nothing():
    odd = album(152)
    odd["genres"]["data"].append({"id": [464]})

    assert tags_for(ISRC, deezer_answering(TRACK, odd)) == ["rock"]


def test_genres_that_are_no_object_are_an_answer_without_tags():
    odd = album(152)
    odd["genres"] = [{"id": 152}]

    assert tags_for(ISRC, deezer_answering(TRACK, odd)) == []


def test_a_track_without_an_album_is_an_answer_without_tags():
    client = deezer_answering({"id": 1, "artist": {"id": 27}})

    assert tags_for(ISRC, client) == []
    assert client.get.await_count == 1


def test_no_isrc_asks_nothing():
    client = deezer_answering()

    assert tags_for(None, client) == []
    client.get.assert_not_called()


def test_any_other_deezer_error_is_no_answer():
    quota = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}

    assert tags_for(ISRC, deezer_answering(quota)) is None


def test_an_album_request_that_fails_is_no_answer():
    assert tags_for(ISRC, deezer_answering(TRACK, httpx.ReadTimeout("slow"))) is None


def test_an_http_error_is_no_answer():
    request = httpx.Request("GET", "https://api.deezer.com/track/isrc:X")
    broken = answer({})
    broken.raise_for_status.side_effect = httpx.HTTPStatusError(
        "502", request=request, response=httpx.Response(502, request=request),
    )
    client = AsyncMock()
    client.get.return_value = broken

    assert tags_for(ISRC, client) is None


def test_an_answer_that_is_not_an_object_is_no_answer():
    assert tags_for(ISRC, deezer_answering(["unexpected"])) is None


def test_no_connection_is_no_answer():
    assert tags_for(ISRC, deezer_answering(error=httpx.ConnectError("no route"))) is None


# Where each Deezer genre is meant to take a track
EXPECTED_PLAYLISTS = {
    132: "TURDOM Pop", 116: "TURDOM Hip-Hop", 152: "TURDOM Rock", 113: "TURDOM Electronic",
    106: "TURDOM Electronic", 111: "TURDOM Electronic", 165: "TURDOM R&B", 169: "TURDOM R&B",
    464: "TURDOM Metal", 466: "TURDOM Indie", 84: "TURDOM Indie", 87: "TURDOM Rock",
    129: "TURDOM Chill", 98: "TURDOM Soundtrack", 173: "TURDOM Soundtrack", 174: "TURDOM Soundtrack",
    197: "TURDOM Pop", 122: "TURDOM Pop",
}


def test_every_mapped_deezer_genre_has_its_intended_playlist():
    from app.services.deezer import _GENRE_TAGS

    assert set(_GENRE_TAGS) == set(EXPECTED_PLAYLISTS)


@pytest.mark.parametrize("genre_id,playlist", sorted(EXPECTED_PLAYLISTS.items()))
def test_a_deezer_genre_leads_to_its_playlist(genre_id, playlist):
    from app.services.deezer import _GENRE_TAGS

    assert classify_track_multi(_GENRE_TAGS[genre_id]) == [playlist]
