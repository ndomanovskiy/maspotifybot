"""Tests for multi-genre classification and hierarchy."""

from app.services.genre_distributor import classify_track, classify_track_multi


# ============================================================
# classify_track_multi — returns all matching playlists
# ============================================================

class TestClassifyTrackMulti:

    def test_single_genre(self):
        assert classify_track_multi("rock") == ["TURDOM Rock"]

    def test_multiple_independent_genres(self):
        """Electronic + Hip-Hop → both playlists."""
        result = classify_track_multi("electronic, hip hop")
        assert "TURDOM Electronic" in result
        assert "TURDOM Hip-Hop" in result

    def test_metal_suppresses_rock(self):
        """Metal > Rock: track goes to Metal only, not Rock."""
        result = classify_track_multi("metal, rock")
        assert "TURDOM Metal" in result
        assert "TURDOM Rock" not in result

    def test_metalcore_suppresses_rock(self):
        """metalcore maps to Metal, which suppresses Rock."""
        result = classify_track_multi("metalcore, rock")
        assert "TURDOM Metal" in result
        assert "TURDOM Rock" not in result

    def test_dnb_suppresses_electronic(self):
        """DnB > Electronic."""
        result = classify_track_multi("drum and bass, electronic")
        assert "TURDOM DnB" in result
        assert "TURDOM Electronic" not in result

    def test_rnb_suppresses_pop(self):
        """R&B > Pop."""
        result = classify_track_multi("r&b, pop")
        assert "TURDOM R&B" in result
        assert "TURDOM Pop" not in result

    def test_rock_only_no_suppression(self):
        """Rock without Metal → goes to Rock."""
        result = classify_track_multi("rock, grunge")
        assert result == ["TURDOM Rock"]

    def test_electronic_only_no_suppression(self):
        """Electronic without DnB → goes to Electronic."""
        result = classify_track_multi("techno, house")
        assert result == ["TURDOM Electronic"]

    def test_three_independent_genres(self):
        """Three different genre families → three playlists."""
        result = classify_track_multi("electronic, hip hop, chill")
        assert len(result) == 3
        assert "TURDOM Electronic" in result
        assert "TURDOM Hip-Hop" in result
        assert "TURDOM Chill" in result

    def test_unknown_genre_empty(self):
        assert classify_track_multi("zambian highlife") == []

    def test_empty_string(self):
        assert classify_track_multi("") == []

    def test_dedup_same_playlist(self):
        """Multiple tags mapping to same playlist → deduplicated."""
        result = classify_track_multi("metal, metalcore, djent")
        assert result == ["TURDOM Metal"]

    def test_progressive_metal_rock(self):
        """progressive metal + rock → Metal only (hierarchy)."""
        result = classify_track_multi("progressive metal, alternative metal, rock")
        assert "TURDOM Metal" in result
        assert "TURDOM Rock" not in result

    def test_indie_and_rock(self):
        """Indie + Rock → both (no hierarchy between them)."""
        result = classify_track_multi("indie, rock")
        assert "TURDOM Indie" in result
        assert "TURDOM Rock" in result


# ============================================================
# classify_track backward compat — returns best single match
# ============================================================

class TestClassifyTrackCompat:

    def test_returns_first(self):
        """classify_track returns first from sorted list."""
        result = classify_track("electronic, hip hop")
        assert result is not None

    def test_none_for_unknown(self):
        assert classify_track("zambian highlife") is None

    def test_single_genre(self):
        assert classify_track("rock") == "TURDOM Rock"


# ============================================================
# Hierarchy edge cases
# ============================================================

class TestHierarchy:

    def test_metal_rock_indie(self):
        """Metal suppresses Rock but not Indie."""
        result = classify_track_multi("metal, rock, indie")
        assert "TURDOM Metal" in result
        assert "TURDOM Rock" not in result
        assert "TURDOM Indie" in result

    def test_dnb_electronic_hiphop(self):
        """DnB suppresses Electronic but not Hip-Hop."""
        result = classify_track_multi("drum and bass, electronic, hip hop")
        assert "TURDOM DnB" in result
        assert "TURDOM Electronic" not in result
        assert "TURDOM Hip-Hop" in result

    def test_no_hierarchy_between_unrelated(self):
        """Pop + Chill → both (no hierarchy)."""
        result = classify_track_multi("pop, chill")
        assert "TURDOM Pop" in result
        assert "TURDOM Chill" in result


# ============================================================
# Real-world tag combinations from Last.fm
# ============================================================

class TestRealWorldTags:

    def test_sleep_token(self):
        """progressive metal, alternative metal, metal, rock."""
        result = classify_track_multi("progressive metal, alternative metal, metal, rock")
        assert "TURDOM Metal" in result
        assert "TURDOM Rock" not in result  # suppressed by Metal

    def test_queen_bohemian_rhapsody(self):
        """classic rock, rock."""
        result = classify_track_multi("classic rock, rock")
        assert "TURDOM Rock" in result

    def test_electronic_chill_combo(self):
        """downtempo, electronic, chill."""
        result = classify_track_multi("downtempo, electronic, chill")
        assert "TURDOM Chill" in result
        assert "TURDOM Electronic" in result

    def test_phonk_standalone(self):
        result = classify_track_multi("phonk")
        assert result == ["TURDOM Phonk"]
