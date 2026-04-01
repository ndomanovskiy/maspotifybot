"""Tests for voting threshold calculation and session participants."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest


# --- Threshold formula tests (no DB needed) ---

def calc_threshold(participant_count: int) -> int:
    """Replicate the threshold formula from voting.py."""
    return max(1, (participant_count + 1) // 2)


class TestThresholdFormula:

    def test_1_participant(self):
        """1 person → threshold 1 (you can drop your own track)."""
        assert calc_threshold(1) == 1

    def test_2_participants(self):
        """2 people → threshold 1 (1 drop needed). ceil(50% of 2) = 1."""
        assert calc_threshold(2) == 1

    def test_3_participants(self):
        """3 people → threshold 2. ceil(50% of 3) = 2."""
        assert calc_threshold(3) == 2

    def test_4_participants(self):
        """4 people → threshold 2. ceil(50% of 4) = 2."""
        assert calc_threshold(4) == 2

    def test_5_participants(self):
        """5 people → threshold 3. ceil(50% of 5) = 3."""
        assert calc_threshold(5) == 3

    def test_6_participants(self):
        assert calc_threshold(6) == 3

    def test_10_participants(self):
        assert calc_threshold(10) == 5

    def test_0_participants_protected(self):
        """0 participants → threshold 1 (max(1, ...) protection)."""
        assert calc_threshold(0) == 1


# --- Session participants DB tests ---

@dataclass
class VotingFakeStore:
    """Minimal fake store for voting tests."""
    session_participants: list[dict] = field(default_factory=list)
    votes: list[dict] = field(default_factory=list)
    session_tracks: list[dict] = field(default_factory=list)
    _next_id: int = 1

    def add_participant(self, session_id: int, telegram_id: int):
        self.session_participants.append({
            "session_id": session_id, "telegram_id": telegram_id
        })

    def add_vote(self, session_track_id: int, telegram_id: int, vote: str):
        self.votes.append({
            "session_track_id": session_track_id,
            "telegram_id": telegram_id,
            "vote": vote,
        })

    def get_participant_count(self, session_id: int) -> int:
        return sum(1 for p in self.session_participants if p["session_id"] == session_id)

    def get_drop_count(self, session_track_id: int) -> int:
        return sum(1 for v in self.votes
                   if v["session_track_id"] == session_track_id and v["vote"] == "drop")


class TestSessionParticipants:

    def test_add_participant(self):
        store = VotingFakeStore()
        store.add_participant(session_id=1, telegram_id=100)
        assert store.get_participant_count(1) == 1

    def test_multiple_participants(self):
        store = VotingFakeStore()
        store.add_participant(session_id=1, telegram_id=100)
        store.add_participant(session_id=1, telegram_id=200)
        store.add_participant(session_id=1, telegram_id=300)
        assert store.get_participant_count(1) == 3

    def test_participants_isolated_by_session(self):
        """Participants from different sessions should not mix."""
        store = VotingFakeStore()
        store.add_participant(session_id=1, telegram_id=100)
        store.add_participant(session_id=1, telegram_id=200)
        store.add_participant(session_id=2, telegram_id=300)
        assert store.get_participant_count(1) == 2
        assert store.get_participant_count(2) == 1

    def test_empty_session(self):
        store = VotingFakeStore()
        assert store.get_participant_count(99) == 0


class TestThresholdWithParticipants:
    """Integration: threshold should be based on session participants, not all users."""

    def test_4_in_session_threshold_2(self):
        store = VotingFakeStore()
        for i in range(4):
            store.add_participant(session_id=1, telegram_id=100 + i)

        count = store.get_participant_count(1)
        threshold = calc_threshold(count)
        assert threshold == 2

    def test_drop_when_threshold_reached(self):
        """With 4 participants (threshold=2), 2 drops should trigger removal."""
        store = VotingFakeStore()
        for i in range(4):
            store.add_participant(session_id=1, telegram_id=100 + i)

        threshold = calc_threshold(store.get_participant_count(1))

        store.add_vote(session_track_id=1, telegram_id=100, vote="drop")
        assert store.get_drop_count(1) < threshold  # 1 < 2

        store.add_vote(session_track_id=1, telegram_id=101, vote="drop")
        assert store.get_drop_count(1) >= threshold  # 2 >= 2 → dropped

    def test_no_drop_when_under_threshold(self):
        """With 5 participants (threshold=3), 2 drops should NOT trigger."""
        store = VotingFakeStore()
        for i in range(5):
            store.add_participant(session_id=1, telegram_id=100 + i)

        threshold = calc_threshold(store.get_participant_count(1))
        assert threshold == 3

        store.add_vote(session_track_id=1, telegram_id=100, vote="drop")
        store.add_vote(session_track_id=1, telegram_id=101, vote="drop")
        assert store.get_drop_count(1) < threshold  # 2 < 3

    def test_keep_votes_dont_count_toward_drop(self):
        store = VotingFakeStore()
        for i in range(4):
            store.add_participant(session_id=1, telegram_id=100 + i)

        threshold = calc_threshold(store.get_participant_count(1))

        store.add_vote(session_track_id=1, telegram_id=100, vote="drop")
        store.add_vote(session_track_id=1, telegram_id=101, vote="keep")
        store.add_vote(session_track_id=1, telegram_id=102, vote="keep")
        assert store.get_drop_count(1) == 1
        assert store.get_drop_count(1) < threshold

    def test_threshold_uses_session_not_global(self):
        """Critical test: threshold must use session participants, not all users."""
        store = VotingFakeStore()

        # Session 1 has 2 participants
        store.add_participant(session_id=1, telegram_id=100)
        store.add_participant(session_id=1, telegram_id=200)

        # Session 2 has 10 participants (should not affect session 1)
        for i in range(10):
            store.add_participant(session_id=2, telegram_id=300 + i)

        session1_threshold = calc_threshold(store.get_participant_count(1))
        session2_threshold = calc_threshold(store.get_participant_count(2))

        assert session1_threshold == 1  # 2 people → threshold 1
        assert session2_threshold == 5  # 10 people → threshold 5

    def test_single_participant_can_drop(self):
        """Solo session: 1 participant, threshold=1, 1 drop = removal."""
        store = VotingFakeStore()
        store.add_participant(session_id=1, telegram_id=100)

        threshold = calc_threshold(store.get_participant_count(1))
        assert threshold == 1

        store.add_vote(session_track_id=1, telegram_id=100, vote="drop")
        assert store.get_drop_count(1) >= threshold
