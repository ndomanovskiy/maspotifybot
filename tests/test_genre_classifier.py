"""Tests for genre classification — whole-word matching, priority, edge cases."""

from app.services.genre_distributor import classify_track


class TestClassifyTrackBasic:

    def test_exact_match(self):
        assert classify_track("rock") == "TURDOM Rock"

    def test_exact_match_metal(self):
        assert classify_track("metal") == "TURDOM Metal"

    def test_exact_match_hip_hop(self):
        assert classify_track("hip hop") == "TURDOM Hip-Hop"

    def test_exact_match_phonk(self):
        assert classify_track("phonk") == "TURDOM Phonk"

    def test_exact_match_drum_and_bass(self):
        assert classify_track("drum and bass") == "TURDOM DnB"

    def test_exact_match_pop(self):
        assert classify_track("pop") == "TURDOM Pop"


class TestClassifyTrackSubstringBug:
    """The original bug: 'hardcore' in 'hardcore hip hop' matched Metal."""

    def test_hardcore_hip_hop_goes_to_hiphop(self):
        """'hardcore hip hop' should match Hip-Hop (via 'hip hop'), not Metal (via 'hardcore')."""
        assert classify_track("hardcore hip hop") == "TURDOM Hip-Hop"

    def test_hardcore_alone_goes_to_metal(self):
        """Plain 'hardcore' should still match Metal."""
        assert classify_track("hardcore") == "TURDOM Metal"

    def test_hardcore_punk_goes_to_metal(self):
        assert classify_track("hardcore punk") == "TURDOM Metal"

    def test_east_coast_hip_hop(self):
        assert classify_track("east coast hip hop") == "TURDOM Hip-Hop"

    def test_old_school_hip_hop(self):
        assert classify_track("old school hip hop") == "TURDOM Hip-Hop"


class TestClassifyTrackMultiGenre:
    """Artist can have multiple genres separated by comma."""

    def test_multiple_genres_picks_best(self):
        result = classify_track("east coast hip hop, old school hip hop, hardcore hip hop")
        assert result == "TURDOM Hip-Hop"

    def test_mixed_metal_and_rock(self):
        result = classify_track("progressive metal, metalcore")
        assert result == "TURDOM Metal"

    def test_nu_metal_variants(self):
        result = classify_track("nu metal, metal, alternative metal, rap metal, rock")
        assert result == "TURDOM Metal"

    def test_punk_and_hardcore_punk(self):
        """'punk' -> Rock, 'hardcore punk' -> Metal (via 'hardcore'). Longest wins."""
        result = classify_track("punk, hardcore punk")
        # 'hardcore punk' has 'hardcore' (1 word Metal match) and 'punk' (1 word Rock match)
        # Both are 1-word matches, first found wins based on GENRE_MAP order
        assert result is not None

    def test_hip_hop_rap_trap(self):
        assert classify_track("hip hop, rap, trap") == "TURDOM Hip-Hop"

    def test_west_coast_gangster_gfunk(self):
        result = classify_track("west coast hip hop, gangster rap, g-funk")
        assert result == "TURDOM Hip-Hop"


class TestClassifyTrackCompoundGenres:
    """Genres with modifiers like 'australian metalcore'."""

    def test_australian_metalcore(self):
        assert classify_track("australian metalcore") == "TURDOM Metal"

    def test_alternative_rock(self):
        assert classify_track("alternative rock") == "TURDOM Rock"

    def test_alternative_metal(self):
        assert classify_track("alternative metal") == "TURDOM Metal"

    def test_future_bass(self):
        assert classify_track("future bass") == "TURDOM DnB"

    def test_liquid_funk(self):
        assert classify_track("liquid funk") == "TURDOM DnB"

    def test_neo_soul(self):
        assert classify_track("neo soul") == "TURDOM R&B"

    def test_indie_rock(self):
        """'indie' is in Indie keywords, 'rock' is in Rock. Both 1 word."""
        result = classify_track("indie rock")
        assert result is not None  # Either Indie or Rock, depending on GENRE_MAP order


class TestClassifyTrackEdgeCases:

    def test_empty_string(self):
        assert classify_track("") is None

    def test_unknown_genre(self):
        assert classify_track("zambian highlife polka") is None

    def test_single_unknown_word(self):
        assert classify_track("blargh") is None

    def test_whitespace_handling(self):
        assert classify_track("  rock  ") == "TURDOM Rock"

    def test_comma_separated_with_spaces(self):
        assert classify_track("pop, rock, electronic") is not None

    def test_case_insensitive(self):
        assert classify_track("Rock") == "TURDOM Rock"
        assert classify_track("METAL") == "TURDOM Metal"
        assert classify_track("Hip Hop") == "TURDOM Hip-Hop"
