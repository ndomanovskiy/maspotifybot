"""Tests for /stats and /mystats logic — genre aggregation, user stats."""

from app.services.genre_distributor import classify_track


class TestStatsGenreAggregation:
    """Test that raw genres from DB correctly aggregate into TURDOM playlists."""

    def _aggregate(self, genre_rows: list[tuple[str, int]]) -> dict[str, int]:
        """Replicate the aggregation logic from /stats handler."""
        genre_totals: dict[str, int] = {}
        for genre, cnt in genre_rows:
            playlist = classify_track(genre)
            if playlist:
                short = playlist.replace("TURDOM ", "")
                genre_totals[short] = genre_totals.get(short, 0) + cnt
        return genre_totals

    def test_single_genre(self):
        result = self._aggregate([("rock", 50)])
        assert result == {"Rock": 50}

    def test_multiple_raw_genres_same_playlist(self):
        """metalcore + metal + djent should all map to Metal."""
        result = self._aggregate([
            ("metalcore", 30),
            ("metal", 20),
            ("djent", 10),
        ])
        assert result == {"Metal": 60}

    def test_multiple_playlists(self):
        result = self._aggregate([
            ("rock", 50),
            ("pop", 40),
            ("hip hop", 30),
        ])
        assert result == {"Rock": 50, "Pop": 40, "Hip-Hop": 30}

    def test_compound_genre_aggregation(self):
        """'alternative rock' should map to Rock, not create separate entry."""
        result = self._aggregate([
            ("rock", 30),
            ("alternative rock", 20),
        ])
        assert result == {"Rock": 50}

    def test_unknown_genres_excluded(self):
        result = self._aggregate([
            ("rock", 50),
            ("zambian highlife", 10),
        ])
        assert result == {"Rock": 50}

    def test_empty_input(self):
        assert self._aggregate([]) == {}

    def test_all_unknown(self):
        result = self._aggregate([("unknown genre xyz", 100)])
        assert result == {}

    def test_hardcore_hip_hop_goes_to_hiphop(self):
        """Regression: hardcore hip hop should aggregate into Hip-Hop."""
        result = self._aggregate([
            ("hardcore hip hop", 15),
            ("hip hop", 25),
        ])
        assert result == {"Hip-Hop": 40}


class TestMyStatsTopGenres:
    """Test top genre selection for /mystats."""

    def _top_n(self, genre_totals: dict[str, int], n: int = 5) -> list[tuple[str, int]]:
        return sorted(genre_totals.items(), key=lambda x: -x[1])[:n]

    def test_top5_from_many(self):
        totals = {"Electronic": 263, "Pop": 232, "DnB": 107, "Rock": 98, "Hip-Hop": 92, "Indie": 76}
        top5 = self._top_n(totals, 5)
        assert len(top5) == 5
        assert top5[0] == ("Electronic", 263)
        assert top5[4] == ("Hip-Hop", 92)
        assert ("Indie", 76) not in top5

    def test_fewer_than_5_genres(self):
        totals = {"Rock": 10, "Pop": 5}
        top5 = self._top_n(totals, 5)
        assert len(top5) == 2

    def test_empty(self):
        assert self._top_n({}, 5) == []

    def test_profile_label(self):
        """Top genre should determine the profile label."""
        totals = {"Metal": 207, "Electronic": 102, "Rock": 99}
        top = self._top_n(totals, 1)
        assert top[0][0] == "Metal"


class TestPerUserGenreBreakdown:
    """Test per-user genre aggregation for /stats message 2."""

    def test_user_genres_isolated(self):
        """Each user's genres should be aggregated independently."""
        # Simulate DB rows: (user, genre, count)
        raw = [
            ("ndomanovskiy", "electronic", 100),
            ("ndomanovskiy", "pop", 80),
            ("k_turanoff", "metalcore", 50),
            ("k_turanoff", "metal", 40),
        ]

        user_genre_map: dict[str, dict[str, int]] = {}
        for name, genre, cnt in raw:
            if name not in user_genre_map:
                user_genre_map[name] = {}
            pl = classify_track(genre)
            if pl:
                short = pl.replace("TURDOM ", "")
                user_genre_map[name][short] = user_genre_map[name].get(short, 0) + cnt

        assert user_genre_map["ndomanovskiy"] == {"Electronic": 100, "Pop": 80}
        assert user_genre_map["k_turanoff"] == {"Metal": 90}

    def test_top3_per_user(self):
        genres = {"Electronic": 263, "Pop": 232, "DnB": 107, "Rock": 98, "Hip-Hop": 92}
        top3 = sorted(genres.items(), key=lambda x: -x[1])[:3]
        assert top3 == [("Electronic", 263), ("Pop", 232), ("DnB", 107)]
