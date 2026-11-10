"""Track display in Telegram HTML: names from Spotify are text, never markup."""

from app.services.track_formatter import build_track_caption, format_track
from app.utils import escape_html


def test_a_missing_value_shows_as_a_question_mark():
    assert escape_html(None) == "?"
    assert escape_html(7) == "7"


def test_a_track_without_title_or_artist_still_formats():
    # session_tracks.title/artist are nullable; a gap must not take a whole message down
    assert format_track(None, None) == "? — <b>?</b>"


def test_track_name_and_artists_are_escaped():
    text = format_track("Rock & Roll <3", "Simon & Garfunkel, <b>Mallory</b>", "id1")

    assert "Simon &amp; Garfunkel, &lt;b&gt;Mallory&lt;/b&gt;" in text
    assert "<b>Rock &amp; Roll &lt;3</b>" in text


def test_track_id_cannot_break_out_of_the_link():
    assert 'href="https://open.spotify.com/track/x&quot;&gt;"' in format_track("Song", "X", 'x">')


def test_track_card_escapes_the_album():
    text = build_track_caption("Song", "X", "Hits & <Misses>", "id1")

    assert "Hits &amp; &lt;Misses&gt;" in text
