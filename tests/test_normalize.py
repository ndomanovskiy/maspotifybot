"""Tests for track title/artist normalization and fuzzy duplicate detection."""

from app.services.normalize import normalize_title, normalize_artist, is_fuzzy_match, _levenshtein


# ============================================================
# normalize_title
# ============================================================

class TestNormalizeTitle:

    def test_simple(self):
        assert normalize_title("Bohemian Rhapsody") == "bohemian rhapsody"

    def test_remastered_suffix(self):
        assert normalize_title("Bohemian Rhapsody - Remastered 2011") == "bohemian rhapsody"

    def test_remastered_parens(self):
        assert normalize_title("Bohemian Rhapsody (Remastered 2011)") == "bohemian rhapsody"

    def test_remastered_no_year(self):
        assert normalize_title("Enter Sandman - Remastered") == "enter sandman"

    def test_year_first_remastered(self):
        assert normalize_title("Enter Sandman (2021 Remastered)") == "enter sandman"

    def test_live_suffix(self):
        assert normalize_title("Comfortably Numb - Live at Wembley") == "comfortably numb"

    def test_live_parens(self):
        assert normalize_title("Nothing Else Matters (Live)") == "nothing else matters"

    def test_feat_parens(self):
        assert normalize_title("Lose Yourself (feat. Eminem)") == "lose yourself"

    def test_ft_suffix(self):
        assert normalize_title("Lose Yourself - ft. Eminem") == "lose yourself"

    def test_with_parens(self):
        assert normalize_title("Song (with Artist)") == "song"

    def test_deluxe(self):
        assert normalize_title("Song (Deluxe Edition)") == "song"

    def test_bonus_track(self):
        assert normalize_title("Song (Bonus Track)") == "song"

    def test_acoustic(self):
        assert normalize_title("Creep (Acoustic)") == "creep"

    def test_unplugged(self):
        assert normalize_title("Layla - Unplugged") == "layla"

    def test_album_version(self):
        assert normalize_title("Song (Album Version)") == "song"

    def test_mono(self):
        assert normalize_title("Let It Be (Mono)") == "let it be"

    def test_stereo_mix(self):
        assert normalize_title("Yesterday - Stereo Mix") == "yesterday"

    def test_remix_kept(self):
        """Remixes are different tracks — should NOT be stripped."""
        result = normalize_title("Song (Remix)")
        assert "remix" in result

    def test_multiple_tags(self):
        assert normalize_title("Song (feat. X) - Remastered 2020") == "song"

    def test_empty(self):
        assert normalize_title("") == ""

    def test_only_tags(self):
        result = normalize_title("- Remastered 2020")
        assert result == ""

    def test_unicode(self):
        assert normalize_title("Für Elise") == "für elise"

    def test_whitespace_collapse(self):
        assert normalize_title("  Two   Spaces  ") == "two spaces"


# ============================================================
# normalize_artist
# ============================================================

class TestNormalizeArtist:

    def test_simple(self):
        assert normalize_artist("Queen") == "queen"

    def test_comma_separated(self):
        assert normalize_artist("Eminem, Dr. Dre") == "eminem dr. dre"

    def test_ampersand(self):
        assert normalize_artist("Simon & Garfunkel") == "simon garfunkel"

    def test_feat(self):
        assert normalize_artist("Drake feat. Rihanna") == "drake rihanna"

    def test_ft(self):
        assert normalize_artist("Drake ft Rihanna") == "drake rihanna"

    def test_and(self):
        assert normalize_artist("Tom and Jerry") == "tom jerry"

    def test_x_in_name_preserved(self):
        """'Lil Nas X' — the X is part of the name, not a separator."""
        assert "lil nas x" in normalize_artist("Lil Nas X")

    def test_semicolon(self):
        assert normalize_artist("Artist1; Artist2") == "artist1 artist2"

    def test_empty(self):
        assert normalize_artist("") == ""


# ============================================================
# is_fuzzy_match
# ============================================================

class TestIsFuzzyMatch:

    def test_identical(self):
        assert is_fuzzy_match("bohemian rhapsody", "bohemian rhapsody", "queen", "queen") == "fuzzy_exact"

    def test_different_artist(self):
        assert is_fuzzy_match("song", "song", "artist a", "artist b") is None

    def test_word_containment(self):
        """Shorter title words all appear in longer title."""
        assert is_fuzzy_match(
            "bohemian rhapsody", "bohemian rhapsody remastered 2011",
            "queen", "queen"
        ) == "fuzzy_contains"

    def test_word_containment_reverse(self):
        assert is_fuzzy_match(
            "bohemian rhapsody remastered 2011", "bohemian rhapsody",
            "queen", "queen"
        ) == "fuzzy_contains"

    def test_single_word_no_contains(self):
        """Single word containment is too weak — don't match."""
        assert is_fuzzy_match("love", "love me do", "beatles", "beatles") is None

    def test_levenshtein_close(self):
        """Typo or minor difference."""
        assert is_fuzzy_match("enter sandman", "enter sandmen", "metallica", "metallica") == "fuzzy_levenshtein"

    def test_levenshtein_too_far(self):
        assert is_fuzzy_match("enter sandman", "exit light", "metallica", "metallica") is None

    def test_completely_different(self):
        assert is_fuzzy_match("song a", "song b which is totally different", "artist", "artist") is None

    def test_empty_titles(self):
        assert is_fuzzy_match("", "", "artist", "artist") == "fuzzy_exact"


# ============================================================
# Real-world test cases
# ============================================================

class TestRealWorldDuplicates:

    def test_remastered_same_track(self):
        a = normalize_title("Bohemian Rhapsody - Remastered 2011")
        b = normalize_title("Bohemian Rhapsody")
        assert a == b

    def test_live_vs_studio(self):
        a = normalize_title("Nothing Else Matters (Live)")
        b = normalize_title("Nothing Else Matters")
        assert a == b

    def test_feat_stripped(self):
        a = normalize_title("Lose Yourself (feat. Eminem)")
        b = normalize_title("Lose Yourself")
        assert a == b

    def test_remix_is_different(self):
        a = normalize_title("Song (Remix)")
        b = normalize_title("Song")
        assert a != b

    def test_full_flow_remastered(self):
        """End-to-end: remastered version detected as fuzzy duplicate."""
        na = normalize_artist("Queen")
        nb = normalize_artist("Queen")
        ta = normalize_title("Bohemian Rhapsody")
        tb = normalize_title("Bohemian Rhapsody - Remastered 2011")
        assert is_fuzzy_match(ta, tb, na, nb) == "fuzzy_exact"

    def test_full_flow_different_songs(self):
        """Different songs by same artist — not a duplicate."""
        na = normalize_artist("Queen")
        nb = normalize_artist("Queen")
        ta = normalize_title("Bohemian Rhapsody")
        tb = normalize_title("We Will Rock You")
        assert is_fuzzy_match(ta, tb, na, nb) is None


# ============================================================
# Levenshtein
# ============================================================

class TestLevenshtein:

    def test_identical(self):
        assert _levenshtein("abc", "abc") == 0

    def test_one_insert(self):
        assert _levenshtein("abc", "abcd") == 1

    def test_one_delete(self):
        assert _levenshtein("abcd", "abc") == 1

    def test_one_replace(self):
        assert _levenshtein("abc", "axc") == 1

    def test_empty(self):
        assert _levenshtein("", "") == 0
        assert _levenshtein("abc", "") == 3
        assert _levenshtein("", "abc") == 3

    def test_completely_different(self):
        assert _levenshtein("abc", "xyz") == 3
