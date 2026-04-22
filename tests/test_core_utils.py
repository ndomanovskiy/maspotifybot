"""Tests for app.bot.core — extract_spotify_id, safe_int, parse_turdom_number, display_name, GENRE_EMOJIS."""

from app.bot.core import extract_spotify_id, safe_int, parse_turdom_number
from app.utils import display_name


# ============================================================
# extract_spotify_id — consolidated from 3 old functions
# ============================================================

class TestExtractTrackId:

    def test_full_url(self):
        url = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=abc"
        assert extract_spotify_id(url, "track") == "6rqhFgbbKwnb9MLmUQDhG6"

    def test_uri(self):
        assert extract_spotify_id("spotify:track:6rqhFgbbKwnb9MLmUQDhG6", "track") == "6rqhFgbbKwnb9MLmUQDhG6"

    def test_raw_id_22_chars(self):
        assert extract_spotify_id("6rqhFgbbKwnb9MLmUQDhG6", "track") == "6rqhFgbbKwnb9MLmUQDhG6"

    def test_raw_id_wrong_length(self):
        assert extract_spotify_id("abc123", "track") is None

    def test_invalid_string(self):
        assert extract_spotify_id("not a spotify link", "track") is None

    def test_empty(self):
        assert extract_spotify_id("", "track") is None

    def test_default_entity_is_track(self):
        url = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
        assert extract_spotify_id(url) == "6rqhFgbbKwnb9MLmUQDhG6"


class TestExtractPlaylistId:

    def test_full_url(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        assert extract_spotify_id(url, "playlist") == "37i9dQZF1DXcBWIGoYBM5M"

    def test_invite_url(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?pt=abc&si=xyz"
        assert extract_spotify_id(url, "playlist") == "37i9dQZF1DXcBWIGoYBM5M"

    def test_raw_id(self):
        assert extract_spotify_id("37i9dQZF1DXcBWIGoYBM5M", "playlist") == "37i9dQZF1DXcBWIGoYBM5M"

    def test_uri(self):
        assert extract_spotify_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "playlist") == "37i9dQZF1DXcBWIGoYBM5M"


class TestExtractUserId:

    def test_full_url(self):
        url = "https://open.spotify.com/user/31xjkjxxfoo?si=abc"
        assert extract_spotify_id(url, "user") == "31xjkjxxfoo"

    def test_raw_username(self):
        assert extract_spotify_id("ndomanovskiy", "user") == "ndomanovskiy"

    def test_raw_username_with_dots(self):
        assert extract_spotify_id("user.name_123", "user") == "user.name_123"

    def test_url_with_slashes_not_raw(self):
        assert extract_spotify_id("some/path", "user") is None

    def test_empty(self):
        assert extract_spotify_id("", "user") is None


class TestExtractCrossEntity:
    """Track URL should not match as playlist and vice versa."""

    def test_track_url_not_playlist(self):
        url = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
        assert extract_spotify_id(url, "playlist") is None

    def test_playlist_url_not_track(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        assert extract_spotify_id(url, "track") is None


# ============================================================
# safe_int — callback data parsing
# ============================================================

class TestSafeInt:

    def test_valid_int(self):
        assert safe_int("42") == 42

    def test_negative(self):
        assert safe_int("-1") == -1

    def test_zero(self):
        assert safe_int("0") == 0

    def test_non_numeric(self):
        assert safe_int("abc") is None

    def test_empty_string(self):
        assert safe_int("") is None

    def test_none(self):
        assert safe_int(None) is None

    def test_float_string(self):
        assert safe_int("3.14") is None

    def test_mixed(self):
        assert safe_int("42abc") is None

    def test_whitespace(self):
        # int() handles leading/trailing whitespace
        assert safe_int(" 42 ") == 42

    def test_very_large(self):
        assert safe_int("999999999999") == 999999999999


# ============================================================
# parse_turdom_number
# ============================================================

class TestParseTurdomNumber:

    def test_normal(self):
        assert parse_turdom_number("/distribute 91") == 91

    def test_with_leading_space(self):
        assert parse_turdom_number("/recap  92 ") == 92

    def test_no_argument(self):
        assert parse_turdom_number("/distribute") is None

    def test_non_numeric(self):
        assert parse_turdom_number("/distribute abc") is None

    def test_zero(self):
        assert parse_turdom_number("/recap 0") == 0

    def test_negative(self):
        assert parse_turdom_number("/recap -1") == -1


# ============================================================
# GENRE_EMOJIS constant (no duplication)
# ============================================================

class TestGenreEmojis:

    def test_imported_from_user_commands(self):
        from app.bot.commands.user import GENRE_EMOJIS
        assert isinstance(GENRE_EMOJIS, dict)
        assert len(GENRE_EMOJIS) == 11

    def test_all_genres_present(self):
        from app.bot.commands.user import GENRE_EMOJIS
        expected = {"Electronic", "Pop", "Metal", "Rock", "Hip-Hop",
                    "Indie", "DnB", "R&B", "Chill", "Soundtrack", "Phonk"}
        assert set(GENRE_EMOJIS.keys()) == expected

    def test_values_are_single_emoji(self):
        from app.bot.commands.user import GENRE_EMOJIS
        for genre, emoji in GENRE_EMOJIS.items():
            assert len(emoji) <= 2, f"{genre} emoji '{emoji}' is too long"


# ============================================================
# display_name
# ============================================================

class TestDisplayName:

    def test_username_preferred(self):
        assert display_name("ndomanovskiy", "Nikita") == "@ndomanovskiy"

    def test_fallback_to_name(self):
        assert display_name(None, "Nikita") == "Nikita"

    def test_empty_username_fallback(self):
        assert display_name("", "Nikita") == "Nikita"

    def test_both_none(self):
        assert display_name(None, None) == "?"

    def test_name_none_username_set(self):
        assert display_name("user", None) == "@user"
