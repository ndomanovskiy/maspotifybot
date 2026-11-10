"""Duplicate-check messages: names from Spotify and users never turn into markup."""

from app.bot.duplicate_messages import (
    auto_removed_text, dropped_before_text, possible_duplicate_text, sibling_text,
)

MATCH = {"match": "fuzzy_exact", "session_status": "keep", "playlist": "Old & <Gold>", "url": "https://x/old",
         "title": "Rock & Roll", "artist": "Simon & Garfunkel"}


def test_possible_duplicate_escapes_every_name():
    text = possible_duplicate_text("Song", "X", "id1", [MATCH], "TURDOM & Co", added_by_name="<Vasya>")

    assert "Old &amp; &lt;Gold&gt;" in text
    assert "Rock &amp; Roll — Simon &amp; Garfunkel" in text
    assert "<b>TURDOM &amp; Co</b>" in text
    assert "&lt;Vasya&gt;" in text
    assert "🔍 нормализованное совпадение" in text


def test_possible_duplicate_that_was_only_dropped_offers_a_second_chance():
    text = possible_duplicate_text("Song", "X", "id1", [dict(MATCH, session_status="drop")], "PL")

    assert "Второй шанс?" in text
    assert "❌ дропнут ранее" in text


def test_no_matches_is_not_a_second_chance():
    assert "Второй шанс" not in possible_duplicate_text("Song", "X", "id1", [], "PL")


def test_a_missing_name_shows_as_a_question_mark():
    drops = [{"playlist": None, "date": "01/02/2026", "added_by": None}]

    text = dropped_before_text("Song", "X", "id1", drops, "PL")

    assert "❌ ? (01/02/2026) — добавил ?" in text


def test_auto_removal_labels_each_match_by_its_kind():
    matches = [dict(MATCH, match="isrc"), MATCH]

    text = auto_removed_text("Song", "X", "id1", matches, "PL & Co", "https://x/pl")

    assert "🔗 ISRC" in text and "🔍 нормализованное совпадение" in text
    assert '<a href="https://x/pl">PL &amp; Co</a>' in text


def test_auto_removal_without_a_playlist_link_shows_the_plain_name():
    text = auto_removed_text("Song", "X", "id1", [dict(MATCH, url=None)], "PL & Co", None)

    assert "Удалён из: PL &amp; Co" in text
    assert 'href="None"' not in text


def test_dropped_before_escapes_playlist_and_who_added():
    drops = [{"playlist": "Old & <Gold>", "date": "01/02/2026", "added_by": "@<vasya>"}]

    text = dropped_before_text("Song", "X", "id1", drops, "PL & Co")

    assert "Old &amp; &lt;Gold&gt; (01/02/2026) — добавил @&lt;vasya&gt;" in text
    assert "<b>PL &amp; Co</b>" in text


def test_sibling_note_escapes_names():
    siblings = [{"kind": "other", "title": "Song (Sped Up)", "artist": "A & B", "playlist": "Old & <Gold>"}]

    text = sibling_text("Song", "X", "id1", siblings, "PL & Co")

    assert "A &amp; B" in text and "Old &amp; &lt;Gold&gt;" in text
    assert "<b>PL &amp; Co</b>" in text
