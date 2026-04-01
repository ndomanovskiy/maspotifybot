"""Tests for /history — pagination logic, session details formatting."""


HISTORY_PAGE_SIZE = 5


class TestHistoryPagination:

    def test_first_page_buttons(self):
        """First page (offset=0) should only have 'next' button if more pages exist."""
        total = 12
        offset = 0
        has_prev = offset > 0
        has_next = offset + HISTORY_PAGE_SIZE < total
        assert has_prev is False
        assert has_next is True

    def test_middle_page_buttons(self):
        """Middle page should have both prev and next buttons."""
        total = 12
        offset = 5
        has_prev = offset > 0
        has_next = offset + HISTORY_PAGE_SIZE < total
        assert has_prev is True
        assert has_next is True

    def test_last_page_buttons(self):
        """Last page should only have 'prev' button."""
        total = 12
        offset = 10
        has_prev = offset > 0
        has_next = offset + HISTORY_PAGE_SIZE < total
        assert has_prev is True
        assert has_next is False

    def test_single_page_no_buttons(self):
        """If total <= page size, no pagination buttons."""
        total = 3
        offset = 0
        has_prev = offset > 0
        has_next = offset + HISTORY_PAGE_SIZE < total
        assert has_prev is False
        assert has_next is False

    def test_exactly_one_page(self):
        total = 5
        offset = 0
        has_prev = offset > 0
        has_next = offset + HISTORY_PAGE_SIZE < total
        assert has_prev is False
        assert has_next is False

    def test_page_range_display(self):
        """Range should show correct 'X-Y of Z'."""
        total = 12
        offset = 5
        start = offset + 1
        end = min(offset + HISTORY_PAGE_SIZE, total)
        assert start == 6
        assert end == 10

    def test_last_page_range(self):
        total = 12
        offset = 10
        start = offset + 1
        end = min(offset + HISTORY_PAGE_SIZE, total)
        assert start == 11
        assert end == 12

    def test_negative_offset_clamped(self):
        """Offset should never go below 0."""
        offset = -5
        clamped = max(0, offset)
        assert clamped == 0


class TestSessionDetailsFormatting:

    def test_track_icons(self):
        """Keep/drop/pending tracks should have correct icons."""
        tracks = [
            {"vote_result": "keep"},
            {"vote_result": "drop"},
            {"vote_result": "pending"},
        ]
        icons = []
        for t in tracks:
            if t["vote_result"] == "keep":
                icons.append("✅")
            elif t["vote_result"] == "drop":
                icons.append("❌")
            else:
                icons.append("⏳")
        assert icons == ["✅", "❌", "⏳"]

    def test_kept_dropped_counts(self):
        tracks = [
            {"vote_result": "keep"},
            {"vote_result": "keep"},
            {"vote_result": "keep"},
            {"vote_result": "drop"},
            {"vote_result": "pending"},
        ]
        kept = sum(1 for t in tracks if t["vote_result"] == "keep")
        dropped = sum(1 for t in tracks if t["vote_result"] == "drop")
        assert kept == 3
        assert dropped == 1
        assert len(tracks) == 5

    def test_empty_session(self):
        tracks = []
        kept = sum(1 for t in tracks if t["vote_result"] == "keep")
        dropped = sum(1 for t in tracks if t["vote_result"] == "drop")
        assert kept == 0
        assert dropped == 0
