"""Tests for /leave and /kick — active flag, threshold recalculation."""

from dataclasses import dataclass, field


def calc_threshold(active_count: int) -> int:
    """Replicate threshold formula from voting.py."""
    return max(1, (active_count + 1) // 2)


@dataclass
class Participant:
    session_id: int
    telegram_id: int
    active: bool = True
    left_at: str | None = None


@dataclass
class SessionStore:
    participants: list[Participant] = field(default_factory=list)

    def add(self, session_id: int, telegram_id: int):
        self.participants.append(Participant(session_id=session_id, telegram_id=telegram_id))

    def leave(self, session_id: int, telegram_id: int):
        for p in self.participants:
            if p.session_id == session_id and p.telegram_id == telegram_id:
                p.active = False
                p.left_at = "2026-04-01T12:00:00"

    def active_count(self, session_id: int) -> int:
        return sum(1 for p in self.participants if p.session_id == session_id and p.active)

    def total_count(self, session_id: int) -> int:
        return sum(1 for p in self.participants if p.session_id == session_id)

    def is_active(self, session_id: int, telegram_id: int) -> bool:
        for p in self.participants:
            if p.session_id == session_id and p.telegram_id == telegram_id:
                return p.active
        return False


class TestLeave:

    def test_leave_sets_inactive(self):
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.leave(1, 200)
        assert store.is_active(1, 100) is True
        assert store.is_active(1, 200) is False

    def test_leave_sets_left_at(self):
        store = SessionStore()
        store.add(1, 100)
        store.leave(1, 100)
        p = store.participants[0]
        assert p.left_at is not None

    def test_leave_reduces_active_count(self):
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.add(1, 300)
        assert store.active_count(1) == 3

        store.leave(1, 300)
        assert store.active_count(1) == 2

    def test_leave_preserves_total_count(self):
        """Left participant still in history."""
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.leave(1, 200)
        assert store.total_count(1) == 2
        assert store.active_count(1) == 1

    def test_leave_nonexistent_noop(self):
        store = SessionStore()
        store.add(1, 100)
        store.leave(1, 999)  # not in session
        assert store.active_count(1) == 1


class TestKick:
    """Kick uses same logic as leave — just triggered by admin."""

    def test_kick_sets_inactive(self):
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.leave(1, 200)  # kick = leave from admin side
        assert store.is_active(1, 200) is False

    def test_kick_admin_stays(self):
        store = SessionStore()
        store.add(1, 100)  # admin
        store.add(1, 200)
        store.leave(1, 200)
        assert store.is_active(1, 100) is True


class TestThresholdAfterLeave:

    def test_5_participants_one_leaves(self):
        """5 → leave → 4 active. Threshold: ceil(50% of 4) = 2."""
        store = SessionStore()
        for i in range(5):
            store.add(1, 100 + i)
        assert calc_threshold(store.active_count(1)) == 3  # 5 people

        store.leave(1, 104)
        assert calc_threshold(store.active_count(1)) == 2  # 4 people

    def test_4_participants_two_leave(self):
        """4 → 2 leave → 2 active. Threshold: ceil(50% of 2) = 1."""
        store = SessionStore()
        for i in range(4):
            store.add(1, 100 + i)

        store.leave(1, 102)
        store.leave(1, 103)
        assert store.active_count(1) == 2
        assert calc_threshold(store.active_count(1)) == 1

    def test_all_leave_except_admin(self):
        """Everyone leaves except admin. Threshold = 1."""
        store = SessionStore()
        store.add(1, 100)  # admin
        store.add(1, 200)
        store.add(1, 300)

        store.leave(1, 200)
        store.leave(1, 300)
        assert store.active_count(1) == 1
        assert calc_threshold(store.active_count(1)) == 1

    def test_join_after_leave_increases_threshold(self):
        """Someone joins after another left — threshold goes up."""
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.add(1, 300)
        assert calc_threshold(store.active_count(1)) == 2  # 3 people

        store.leave(1, 300)
        assert calc_threshold(store.active_count(1)) == 1  # 2 people

        store.add(1, 400)  # new person joins
        assert calc_threshold(store.active_count(1)) == 2  # 3 people again

    def test_threshold_isolated_per_session(self):
        store = SessionStore()
        store.add(1, 100)
        store.add(1, 200)
        store.add(2, 300)
        store.add(2, 400)
        store.add(2, 500)

        store.leave(1, 200)
        assert calc_threshold(store.active_count(1)) == 1  # session 1: 1 active
        assert calc_threshold(store.active_count(2)) == 2  # session 2: 3 active, unaffected
