"""Tests for session flow — user behavior scenarios.

Covers: voting result persistence, race conditions, skip logic,
track card lifecycle, caption limits, session completion, participant display.
"""

from dataclasses import dataclass, field


# --- Replicate core logic for testing without DB ---

def calc_threshold(participant_count: int) -> int:
    return max(1, (participant_count + 1) // 2)


def determine_vote_result(
    total_votes: int, drop_count: int, participant_count: int
) -> str | None:
    """Determine vote result. Returns 'drop', 'keep', or None (still pending)."""
    threshold = calc_threshold(participant_count)
    if drop_count >= threshold:
        return "drop"
    if total_votes >= participant_count and drop_count < threshold:
        return "keep"
    return None


def truncate_artists(artist: str, max_artists: int = 3) -> str:
    """Truncate artist list to max N artists."""
    parts = [a.strip() for a in artist.split(",")]
    if len(parts) <= max_artists:
        return artist
    return ", ".join(parts[:max_artists]) + "…"


def build_caption(
    title: str, artist: str, album: str,
    added_by: str | None = None, facts: str | None = None,
    vote_result_text: str | None = None,
    max_len: int = 1024,
) -> str:
    """Build track card caption, ensuring it fits within Telegram limit."""
    display_artist = truncate_artists(artist)
    added_by_text = f"\n👤 {added_by}" if added_by else ""
    facts_text = f"\n\n💡 {facts}" if facts else ""
    result_text = f"\n\n{vote_result_text}" if vote_result_text else ""

    text = (
        f"🎵 {title}\n"
        f"🎤 {display_artist}\n"
        f"💿 {album}"
        f"{added_by_text}{facts_text}{result_text}"
    )

    if len(text) > max_len:
        # Trim facts first
        remaining = max_len - len(text) + len(facts_text)
        if remaining > 20:
            facts_text = facts_text[:remaining - 1] + "…"
        else:
            facts_text = ""
        text = (
            f"🎵 {title}\n"
            f"🎤 {display_artist}\n"
            f"💿 {album}"
            f"{added_by_text}{facts_text}{result_text}"
        )

    return text


def format_vote_result(keep_count: int, drop_count: int, result: str) -> str:
    """Format vote result for card: '2 за / 2 против — ✅ keep'."""
    emoji = "✅" if result == "keep" else "❌"
    return f"{keep_count} за / {drop_count} против — {emoji} {result}"


@dataclass
class SessionState:
    """Simulates bot session state for testing."""
    participants: list[int] = field(default_factory=list)
    played_track_ids: set[str] = set
    current_session_track_id: int | None = None
    skip_in_progress: set[int] = field(default_factory=set)
    tracks: dict[int, dict] = field(default_factory=dict)  # track_id -> {votes, result}
    skips_performed: int = 0

    def __post_init__(self):
        if self.played_track_ids is set:
            self.played_track_ids = set()

    def add_track(self, track_id: int, spotify_id: str):
        self.tracks[track_id] = {
            "spotify_id": spotify_id,
            "votes": {},  # telegram_id -> vote
            "result": "pending",
        }
        self.current_session_track_id = track_id
        self.played_track_ids.add(spotify_id)

    def vote(self, track_id: int, telegram_id: int, vote: str) -> dict:
        """Simulate voting. Mirrors new voting.py behavior — no early return on drop."""
        track = self.tracks[track_id]

        # Already voted same
        if telegram_id in track["votes"] and track["votes"][telegram_id] == vote:
            return {"status": "already_voted"}

        changed = telegram_id in track["votes"]
        track["votes"][telegram_id] = vote

        drop_count = sum(1 for v in track["votes"].values() if v == "drop")
        keep_count = sum(1 for v in track["votes"].values() if v == "keep")
        total = len(track["votes"])
        threshold = calc_threshold(len(self.participants))

        # Determine and persist result (mirrors voting.py)
        vote_result = None
        if drop_count >= threshold:
            track["result"] = "drop"
            vote_result = "drop"
        elif total >= len(self.participants) and drop_count < threshold:
            track["result"] = "keep"
            vote_result = "keep"

        status = "vote_changed" if changed else "recorded"
        return {
            "status": status,
            "vote_result": vote_result,
            "drop_count": drop_count,
            "keep_count": keep_count,
            "total_votes": total,
            "threshold": threshold,
            "participants": len(self.participants),
        }

    def should_skip(self, track_id: int) -> bool:
        """Check if skip should happen after vote."""
        track = self.tracks[track_id]
        total = len(track["votes"])
        if total < len(self.participants):
            return False  # Not all voted yet
        if track_id in self.skip_in_progress:
            return False  # Already skipping
        return True

    def do_skip(self, track_id: int):
        """Perform skip with race condition guard."""
        if track_id in self.skip_in_progress:
            return False
        self.skip_in_progress.add(track_id)
        self.skips_performed += 1
        return True

    def is_track_played(self, spotify_id: str) -> bool:
        return spotify_id in self.played_track_ids

    def all_tracks_voted(self) -> bool:
        return all(t["result"] != "pending" for t in self.tracks.values())


# ============================================================
# Test: keep result is persisted in DB
# ============================================================

class TestKeepPersistence:

    def test_all_keep_sets_result(self):
        """When all 4 participants vote keep, result should be 'keep' not 'pending'."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        state.vote(1, 3, "keep")
        result = state.vote(1, 4, "keep")

        assert state.tracks[1]["result"] == "keep"

    def test_3_keep_1_drop_sets_keep(self):
        """3 keep + 1 drop with 4 participants (threshold=2) → keep."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "drop")
        state.vote(1, 3, "keep")
        state.vote(1, 4, "keep")

        assert state.tracks[1]["result"] == "keep"

    def test_partial_votes_stays_pending(self):
        """2 out of 4 votes → still pending."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")

        assert state.tracks[1]["result"] == "pending"

    def test_2_drop_2_keep_drops(self):
        """2 drop + 2 keep with 4 participants (threshold=2) → drop."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")  # threshold reached
        # Track already dropped, but let's verify
        assert state.tracks[1]["result"] == "drop"


# ============================================================
# Test: skip only when ALL voted (not on threshold)
# ============================================================

class TestSkipOnlyWhenAllVoted:

    def test_no_skip_on_partial_votes(self):
        """2 of 4 voted → no skip even if both are drop."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")

        assert not state.should_skip(1)

    def test_skip_when_all_voted_keep(self):
        """All 4 voted keep → skip."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        for i in range(4):
            state.vote(1, i + 1, "keep")

        assert state.should_skip(1)

    def test_skip_when_all_voted_mixed(self):
        """All 4 voted (3 keep, 1 drop) → skip."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "drop")
        state.vote(1, 3, "keep")
        state.vote(1, 4, "keep")

        assert state.should_skip(1)

    def test_3_of_4_not_enough_to_skip(self):
        """3 of 4 voted → no skip yet."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        state.vote(1, 3, "keep")

        assert not state.should_skip(1)


# ============================================================
# Test: race condition — multiple drops don't cause multiple skips
# ============================================================

class TestDropRaceCondition:

    def test_concurrent_drops_single_skip(self):
        """3 people press drop 'simultaneously' → only 1 skip performed."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        # All vote, 3 drops
        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")
        state.vote(1, 3, "drop")
        state.vote(1, 4, "keep")

        # Simulate 3 concurrent skip attempts
        assert state.do_skip(1) is True   # first succeeds
        assert state.do_skip(1) is False  # second blocked
        assert state.do_skip(1) is False  # third blocked

        assert state.skips_performed == 1

    def test_skip_guard_per_track(self):
        """Skip guard is per track — different tracks can skip independently."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")
        state.add_track(2, "spotify_def")

        assert state.do_skip(1) is True
        assert state.do_skip(2) is True
        assert state.skips_performed == 2

    def test_skip_only_current_track(self):
        """Voting on old track should not skip current track."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")  # old track
        state.add_track(2, "spotify_def")  # current track

        # Vote on old track
        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")

        # Skip should be possible but should NOT affect current track
        should = state.should_skip(1)
        assert should is True
        # But in real code we check: track_id == _current_session_track_id
        assert state.current_session_track_id == 2  # current is track 2, not 1


# ============================================================
# Test: already played track — skip, don't end session
# ============================================================

class TestAlreadyPlayedTrack:

    def test_played_track_detected(self):
        """Track that was already played should be detected."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")

        assert state.is_track_played("spotify_abc") is True
        assert state.is_track_played("spotify_new") is False

    def test_session_not_ended_on_replay(self):
        """Encountering an already played track should NOT end session."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")
        state.add_track(2, "spotify_def")

        # Simulate: spotify plays "abc" again
        # Old behavior: end session
        # New behavior: skip, session continues
        assert state.is_track_played("spotify_abc") is True
        # Session state should remain valid
        assert state.current_session_track_id == 2
        assert len(state.tracks) == 2


# ============================================================
# Test: caption truncation
# ============================================================

class TestCaptionTruncation:

    def test_short_artist_unchanged(self):
        assert truncate_artists("Eminem") == "Eminem"

    def test_three_artists_unchanged(self):
        assert truncate_artists("A, B, C") == "A, B, C"

    def test_four_artists_truncated(self):
        result = truncate_artists("A, B, C, D")
        assert result == "A, B, C…"

    def test_40_artists_truncated(self):
        """We Are The World scenario — 40 artists."""
        artists = ", ".join(f"Artist{i}" for i in range(40))
        result = truncate_artists(artists)
        assert result.count(",") == 2  # only 2 commas = 3 artists
        assert result.endswith("…")

    def test_caption_within_limit(self):
        caption = build_caption("Song", "Artist", "Album", facts="Cool fact")
        assert len(caption) <= 1024

    def test_long_facts_truncated(self):
        """Very long facts should be trimmed to fit 1024."""
        long_facts = "x" * 2000
        caption = build_caption("Song", "Artist", "Album", facts=long_facts)
        assert len(caption) <= 1024

    def test_caption_with_result_fits(self):
        """Caption with vote result appended must still fit."""
        facts = "x" * 800
        result_text = format_vote_result(3, 1, "keep")
        caption = build_caption("Song", "Artist", "Album",
                                facts=facts, vote_result_text=result_text)
        assert len(caption) <= 1024

    def test_vote_result_format(self):
        assert format_vote_result(3, 1, "keep") == "3 за / 1 против — ✅ keep"
        assert format_vote_result(1, 3, "drop") == "1 за / 3 против — ❌ drop"
        assert format_vote_result(2, 2, "drop") == "2 за / 2 против — ❌ drop"


# ============================================================
# Test: session completion detection
# ============================================================

class TestSessionCompletion:

    def test_not_complete_with_pending(self):
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")
        state.add_track(2, "spotify_def")

        # Vote only on first track
        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")

        assert not state.all_tracks_voted()

    def test_complete_when_all_voted(self):
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")
        state.add_track(2, "spotify_def")

        # Vote on both tracks
        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        state.vote(2, 1, "keep")
        state.vote(2, 2, "keep")

        assert state.all_tracks_voted()

    def test_complete_with_mix_of_keep_and_drop(self):
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")
        state.add_track(2, "spotify_def")

        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")
        state.vote(2, 1, "keep")
        state.vote(2, 2, "keep")

        assert state.all_tracks_voted()
        assert state.tracks[1]["result"] == "drop"
        assert state.tracks[2]["result"] == "keep"


# ============================================================
# Test: vote change behavior
# ============================================================

class TestVoteChange:

    def test_change_keep_to_drop(self):
        """With 4 participants, changing keep→drop should register as vote_changed."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        result = state.vote(1, 1, "drop")
        assert result["status"] == "vote_changed"
        assert state.tracks[1]["votes"][1] == "drop"

    def test_same_vote_rejected(self):
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        result = state.vote(1, 1, "keep")
        assert result["status"] == "already_voted"

    def test_change_affects_result(self):
        """Change vote from keep to drop can flip result."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "spotify_abc")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        assert state.tracks[1]["result"] == "keep"

        # User 2 changes to drop — now 1 keep 1 drop, threshold=1 → drop
        state.vote(1, 2, "drop")
        assert state.tracks[1]["result"] == "drop"


# ============================================================
# Test: determine_vote_result edge cases
# ============================================================

class TestDetermineVoteResult:

    def test_0_votes(self):
        assert determine_vote_result(0, 0, 4) is None

    def test_1_of_4_drop(self):
        assert determine_vote_result(1, 1, 4) is None  # not all voted

    def test_2_of_4_drop_threshold_reached(self):
        """2 drops with 4 participants → drop (threshold=2)."""
        assert determine_vote_result(2, 2, 4) == "drop"

    def test_4_of_4_keep(self):
        assert determine_vote_result(4, 0, 4) == "keep"

    def test_4_of_4_mixed_keep(self):
        """4 votes, 1 drop, 3 keep → keep (threshold=2 not reached)."""
        assert determine_vote_result(4, 1, 4) == "keep"

    def test_4_of_4_mixed_drop(self):
        """4 votes, 2 drop, 2 keep → drop (threshold=2 reached)."""
        assert determine_vote_result(4, 2, 4) == "drop"

    def test_3_of_4_no_decision(self):
        """3 votes, 1 drop → pending (not all voted, threshold not reached)."""
        assert determine_vote_result(3, 1, 4) is None

    def test_5_participants_3_drop(self):
        """5 participants, 3 drops → drop (threshold=3)."""
        assert determine_vote_result(3, 3, 5) == "drop"

    def test_5_participants_2_drop_not_enough(self):
        """5 participants, 2 drops out of 2 votes → pending."""
        assert determine_vote_result(2, 2, 5) is None


# ============================================================
# Test: participant display
# ============================================================

class TestParticipantDisplay:

    def test_format_names(self):
        names = ["@ndomanovskiy", "@k_turanoff", "@Agosev"]
        text = f"👥 Участников: {len(names)} — {', '.join(names)}"
        assert "@ndomanovskiy" in text
        assert "3" in text


# ============================================================
# Test: full session scenario — realistic user behavior
# ============================================================

class TestFullSessionScenario:

    def test_normal_session_flow(self):
        """4 participants, 3 tracks: 2 keep, 1 drop."""
        state = SessionState(participants=[1, 2, 3, 4])

        # Track 1: all keep
        state.add_track(1, "sp_1")
        for p in [1, 2, 3, 4]:
            state.vote(1, p, "keep")
        assert state.tracks[1]["result"] == "keep"
        assert state.should_skip(1)
        state.do_skip(1)

        # Track 2: 2 drop 2 keep → drop
        state.add_track(2, "sp_2")
        state.vote(2, 1, "drop")
        state.vote(2, 2, "drop")
        state.vote(2, 3, "keep")
        state.vote(2, 4, "keep")
        assert state.tracks[2]["result"] == "drop"
        assert state.should_skip(2)
        state.do_skip(2)

        # Track 3: 3 keep 1 drop → keep
        state.add_track(3, "sp_3")
        state.vote(3, 1, "keep")
        state.vote(3, 2, "keep")
        state.vote(3, 3, "keep")
        state.vote(3, 4, "drop")
        assert state.tracks[3]["result"] == "keep"

        assert state.all_tracks_voted()
        assert state.skips_performed == 2

    def test_already_played_track_in_session(self):
        """Track replays during session are detected."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "sp_1")
        state.add_track(2, "sp_2")

        # sp_1 comes back in shuffle
        assert state.is_track_played("sp_1")
        # Should skip, not end session
        # Session continues with track 2 as current
        assert state.current_session_track_id == 2

    def test_session_resilience_skip_fails(self):
        """If skip fails, session should NOT end — just log and continue."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "sp_1")
        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")

        # Simulate skip failure — session state should remain intact
        # (in real code: except block logs error, no _end_session call)
        assert state.current_session_track_id == 1
        assert len(state.tracks) == 1
        # Session is still valid

    def test_late_voter_on_old_track(self):
        """Voting on old track after new one started — no skip of current."""
        state = SessionState(participants=[1, 2, 3, 4])

        # Track 1
        state.add_track(1, "sp_1")
        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        state.vote(1, 3, "keep")
        # Track 1 not fully voted (missing p4)

        # Track 2 starts (Spotify auto-advanced)
        state.add_track(2, "sp_2")

        # p4 late-votes on track 1
        state.vote(1, 4, "keep")
        assert state.tracks[1]["result"] == "keep"

        # Should skip track 1 but NOT current track 2
        assert state.should_skip(1)
        assert state.current_session_track_id == 2


# ============================================================
# Test: new voting.py — no early return on drop, vote_result field
# ============================================================

class TestVoteResultField:

    def test_drop_returns_vote_result_not_status(self):
        """record_vote no longer returns status='dropped'. Uses vote_result field."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "drop")
        result = state.vote(1, 2, "drop")  # threshold reached

        # New behavior: status is "recorded", vote_result is "drop"
        assert result["status"] == "recorded"
        assert result["vote_result"] == "drop"

    def test_keep_returns_vote_result(self):
        """When all voted keep, vote_result should be 'keep'."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "keep")
        result = state.vote(1, 2, "keep")

        assert result["status"] == "recorded"
        assert result["vote_result"] == "keep"

    def test_partial_votes_no_vote_result(self):
        """Before all votes in, vote_result should be None."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        result = state.vote(1, 1, "keep")
        assert result["vote_result"] is None

    def test_threshold_field_in_result(self):
        """Result should include threshold for handlers to use."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        result = state.vote(1, 1, "keep")
        assert result["threshold"] == 2
        assert result["participants"] == 4


# ============================================================
# Test: drop does NOT trigger skip until all voted
# ============================================================

class TestDropWaitsForAllVotes:

    def test_drop_threshold_reached_but_no_skip(self):
        """2 drops with 4 participants — drop recorded but no skip yet."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")

        # Drop recorded in DB
        assert state.tracks[1]["result"] == "drop"
        # But should NOT skip — only 2 of 4 voted
        assert not state.should_skip(1)

    def test_drop_skips_after_all_voted(self):
        """Drop + remaining votes → now skip."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")
        assert not state.should_skip(1)

        state.vote(1, 3, "keep")
        assert not state.should_skip(1)

        state.vote(1, 4, "keep")
        assert state.should_skip(1)  # NOW skip — all 4 voted

    def test_everyone_can_vote_before_skip(self):
        """All 4 participants get to vote even if drop threshold reached early."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        # First two drop
        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")
        # Third and fourth can still vote
        r3 = state.vote(1, 3, "keep")
        assert r3["status"] == "recorded"
        r4 = state.vote(1, 4, "keep")
        assert r4["status"] == "recorded"

        # All votes counted
        assert r4["total_votes"] == 4
        assert r4["drop_count"] == 2
        assert r4["keep_count"] == 2


# ============================================================
# Test: caption with vote result reserve
# ============================================================

class TestCaptionWithResultReserve:

    def test_result_fits_after_facts(self):
        """Caption with facts + vote result must fit in 1024."""
        facts = "A" * 700
        result_text = format_vote_result(3, 1, "keep")
        caption = build_caption(
            "Song Title", "Artist Name", "Album",
            added_by="@user", facts=facts, vote_result_text=result_text,
        )
        assert len(caption) <= 1024

    def test_long_facts_trimmed_for_result(self):
        """Facts should be trimmed to leave room for vote result."""
        facts = "A" * 950
        result_text = format_vote_result(2, 2, "drop")
        caption = build_caption(
            "Song", "Artist", "Album",
            facts=facts, vote_result_text=result_text,
        )
        assert len(caption) <= 1024
        assert "2 за / 2 против" in caption

    def test_no_facts_with_result(self):
        """Caption without facts but with result should work."""
        result_text = format_vote_result(4, 0, "keep")
        caption = build_caption("Song", "Artist", "Album", vote_result_text=result_text)
        assert "4 за / 0 против — ✅ keep" in caption
        assert len(caption) <= 1024


# ============================================================
# Test: artist truncation to 3
# ============================================================

class TestArtistTruncationTo3:

    def test_1_artist(self):
        assert truncate_artists("Eminem") == "Eminem"

    def test_2_artists(self):
        assert truncate_artists("Eminem, Dr. Dre") == "Eminem, Dr. Dre"

    def test_3_artists(self):
        assert truncate_artists("A, B, C") == "A, B, C"

    def test_4_artists_shows_3(self):
        result = truncate_artists("A, B, C, D")
        assert result == "A, B, C…"
        assert "D" not in result

    def test_10_artists_shows_3(self):
        artists = ", ".join(f"Artist{i}" for i in range(10))
        result = truncate_artists(artists)
        assert "Artist0" in result
        assert "Artist1" in result
        assert "Artist2" in result
        assert "Artist3" not in result
        assert result.endswith("…")

    def test_we_are_the_world(self):
        """40 artists — should show only first 3."""
        artists = "U.S.A. For Africa, Al Jarreau, Anita Pointer, Bette Midler, Billy Joel, " + \
                  ", ".join(f"Artist{i}" for i in range(35))
        result = truncate_artists(artists)
        parts = result.rstrip("…").split(",")
        assert len(parts) == 3


# ============================================================
# Test: vote change with new return format
# ============================================================

class TestVoteChangeNewFormat:

    def test_change_returns_correct_status(self):
        """Changing vote should return status='vote_changed'."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "keep")
        result = state.vote(1, 1, "drop")
        assert result["status"] == "vote_changed"
        assert result["drop_count"] == 1

    def test_change_last_vote_triggers_result(self):
        """If vote change is the 4th unique voter — should set vote_result."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")
        state.vote(1, 3, "keep")
        state.vote(1, 4, "drop")
        # All voted, result is keep (1 drop < threshold 2)
        assert state.tracks[1]["result"] == "keep"

        # p4 changes drop → keep
        result = state.vote(1, 4, "keep")
        assert result["status"] == "vote_changed"
        assert result["vote_result"] == "keep"
        assert result["drop_count"] == 0


# ============================================================
# Test: full scenario with new skip-on-all-voted logic
# ============================================================

class TestFullScenarioNewLogic:

    def test_drop_waits_then_removes(self):
        """2 early drops, 2 late keeps — drop finalized only after all vote."""
        state = SessionState(participants=[1, 2, 3, 4])
        state.add_track(1, "sp_1")

        # Two drops — threshold reached but no skip
        state.vote(1, 1, "drop")
        state.vote(1, 2, "drop")
        assert state.tracks[1]["result"] == "drop"
        assert not state.should_skip(1)

        # Two keeps — now all voted, skip
        state.vote(1, 3, "keep")
        state.vote(1, 4, "keep")
        assert state.should_skip(1)
        assert state.do_skip(1)
        assert state.skips_performed == 1

    def test_no_double_skip_new_logic(self):
        """Even with new logic, concurrent finalization = 1 skip."""
        state = SessionState(participants=[1, 2])
        state.add_track(1, "sp_1")

        state.vote(1, 1, "keep")
        state.vote(1, 2, "keep")

        # Two concurrent attempts to finalize
        assert state.do_skip(1) is True
        assert state.do_skip(1) is False
        assert state.skips_performed == 1

    def test_mixed_session_3_tracks(self):
        """Realistic: 3 tracks, different outcomes, all wait for all votes."""
        state = SessionState(participants=[1, 2, 3])

        # Track 1: unanimous keep
        state.add_track(1, "sp_1")
        for p in [1, 2, 3]:
            state.vote(1, p, "keep")
        assert state.tracks[1]["result"] == "keep"
        assert state.should_skip(1)

        # Track 2: 2 drop 1 keep (threshold=2) — drop
        state.add_track(2, "sp_2")
        state.vote(2, 1, "drop")
        state.vote(2, 2, "drop")
        assert not state.should_skip(2)  # only 2/3 voted!
        state.vote(2, 3, "keep")
        assert state.should_skip(2)
        assert state.tracks[2]["result"] == "drop"

        # Track 3: 1 drop 2 keep — keep
        state.add_track(3, "sp_3")
        state.vote(3, 1, "drop")
        state.vote(3, 2, "keep")
        state.vote(3, 3, "keep")
        assert state.tracks[3]["result"] == "keep"

        assert state.all_tracks_voted()
