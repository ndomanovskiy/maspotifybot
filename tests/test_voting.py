"""Tests for voting threshold calculation and session participants."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.voting import create_session_track


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


# --- create_session_track: added_by fallback ---

# Positional arg index of added_by_spotify_id in the INSERT INTO session_tracks call.
# Order: session_id, track_id, spotify_track_id, title, artist, album, cover_url, added_by
_INSERT_ADDED_BY_ARG = 7


def _make_track_info(added_by: str | None):
    return SimpleNamespace(
        track_id="track_abc",
        title="Song",
        artist="Artist",
        album="Album",
        cover_url="https://cdn/cover.jpg",
        added_by=added_by,
    )


def _make_pool(playlist_added_by: str | None):
    """Build a fake pool whose connection answers the queries that
    create_session_track issues. Returns (pool, calls) where calls
    is a list of (method, query, args) tuples for assertions."""
    calls: list = []

    async def fetchval(query, *args):
        calls.append(("fetchval", query, args))
        q = query.lower()
        if "insert into tracks" in q:
            return 42  # track_db_id
        if "from playlist_tracks" in q and "added_by_spotify_id" in q:
            return playlist_added_by
        return None

    async def fetchrow(query, *args):
        calls.append(("fetchrow", query, args))
        return {"id": 777}

    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    return pool, calls


def _fallback_lookups(calls):
    return [c for c in calls
            if c[0] == "fetchval"
            and "from playlist_tracks" in c[1].lower()
            and "added_by_spotify_id" in c[1].lower()]


def _insert_into_session_tracks(calls):
    return [c for c in calls
            if c[0] == "fetchrow" and "insert into session_tracks" in c[1].lower()]


class TestCreateSessionTrackFallback:
    """Regression: monitor sometimes returns added_by=None; we must fall back
    to playlist_tracks.added_by_spotify_id so session_tracks doesn't lose the
    attribution. Bug seen in production session 13 (TURDOM#98)."""

    def test_fallback_used_when_monitor_returned_none(self):
        pool, calls = _make_pool(playlist_added_by="user_from_pt")
        info = _make_track_info(added_by=None)

        session_track_id, resolved = asyncio.run(
            create_session_track(pool, session_id=99, track_info=info)
        )

        # Fallback query should have run exactly once with (session_id, track_id)
        lookups = _fallback_lookups(calls)
        assert len(lookups) == 1
        assert lookups[0][2] == (99, "track_abc")

        # INSERT uses the fallback value
        inserts = _insert_into_session_tracks(calls)
        assert len(inserts) == 1
        assert inserts[0][2][_INSERT_ADDED_BY_ARG] == "user_from_pt"

        # Return tuple exposes the resolved value to the caller
        assert session_track_id == 777
        assert resolved == "user_from_pt"

    def test_no_fallback_when_monitor_provided_added_by(self):
        pool, calls = _make_pool(playlist_added_by="should_not_use_this")
        info = _make_track_info(added_by="user_from_monitor")

        _, resolved = asyncio.run(
            create_session_track(pool, session_id=99, track_info=info)
        )

        assert _fallback_lookups(calls) == []
        inserts = _insert_into_session_tracks(calls)
        assert inserts[0][2][_INSERT_ADDED_BY_ARG] == "user_from_monitor"
        assert resolved == "user_from_monitor"

    def test_added_by_stays_null_when_both_sources_empty(self):
        """If playlist_tracks also has no record (orphan track), we accept NULL."""
        pool, calls = _make_pool(playlist_added_by=None)
        info = _make_track_info(added_by=None)

        _, resolved = asyncio.run(
            create_session_track(pool, session_id=99, track_info=info)
        )

        inserts = _insert_into_session_tracks(calls)
        assert inserts[0][2][_INSERT_ADDED_BY_ARG] is None
        assert resolved is None

    def test_empty_string_normalized_to_none_and_triggers_fallback(self):
        """Empty spotify_id is never valid. Normalize to None and run fallback
        so we don't end up with garbage '' in session_tracks."""
        pool, calls = _make_pool(playlist_added_by="user_from_pt")
        info = _make_track_info(added_by="")

        _, resolved = asyncio.run(
            create_session_track(pool, session_id=99, track_info=info)
        )

        # Fallback should have run
        assert len(_fallback_lookups(calls)) == 1
        # INSERT receives the fallback value, not ''
        inserts = _insert_into_session_tracks(calls)
        assert inserts[0][2][_INSERT_ADDED_BY_ARG] == "user_from_pt"
        assert resolved == "user_from_pt"

    def test_empty_string_with_no_fallback_becomes_none(self):
        """When both the monitor and playlist_tracks have no value, store NULL
        — never ''."""
        pool, calls = _make_pool(playlist_added_by=None)
        info = _make_track_info(added_by="")

        _, resolved = asyncio.run(
            create_session_track(pool, session_id=99, track_info=info)
        )

        inserts = _insert_into_session_tracks(calls)
        assert inserts[0][2][_INSERT_ADDED_BY_ARG] is None
        assert resolved is None
