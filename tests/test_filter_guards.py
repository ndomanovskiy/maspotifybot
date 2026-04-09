"""Tests for lambda filter guards — m.text None safety, command interception prevention."""

from dataclasses import dataclass


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeMessage:
    """Minimal message mock for filter testing."""
    text: str | None
    from_user: FakeUser


# ============================================================
# Secret clarification filter: m.text guard
# ============================================================

class TestSecretClarificationFilter:
    """Filter: lambda m: m.text and m.from_user.id in waiting and not m.text.startswith('/')"""

    def _filter(self, m: FakeMessage, waiting: dict) -> bool:
        """Replicate the filter from user.py."""
        return bool(m.text and m.from_user.id in waiting and not m.text.startswith("/"))

    def test_normal_text_in_waiting(self):
        m = FakeMessage(text="уточнение", from_user=FakeUser(id=123))
        assert self._filter(m, {123: {}}) is True

    def test_command_not_intercepted(self):
        m = FakeMessage(text="/start", from_user=FakeUser(id=123))
        assert self._filter(m, {123: {}}) is False

    def test_not_in_waiting(self):
        m = FakeMessage(text="hello", from_user=FakeUser(id=456))
        assert self._filter(m, {123: {}}) is False

    def test_none_text_no_crash(self):
        """Photo/sticker sends m.text=None — must not crash."""
        m = FakeMessage(text=None, from_user=FakeUser(id=123))
        assert self._filter(m, {123: {}}) is False

    def test_empty_text(self):
        m = FakeMessage(text="", from_user=FakeUser(id=123))
        assert self._filter(m, {123: {}}) is False

    def test_slash_only(self):
        m = FakeMessage(text="/", from_user=FakeUser(id=123))
        assert self._filter(m, {123: {}}) is False


# ============================================================
# Theme input filter: m.text guard + command guard
# ============================================================

class TestThemeInputFilter:
    """Filter: lambda m: m.text and not m.text.startswith('/') and waiting_theme and is_admin(m)"""

    def _filter(self, m: FakeMessage, waiting_theme: bool, admin_id: int) -> bool:
        """Replicate the filter from __init__.py."""
        return bool(m.text and not m.text.startswith("/") and waiting_theme and m.from_user.id == admin_id)

    def test_normal_theme(self):
        m = FakeMessage(text="Summer Vibes", from_user=FakeUser(id=100))
        assert self._filter(m, True, 100) is True

    def test_command_not_intercepted(self):
        """Admin sends /start while waiting_theme — must NOT be captured."""
        m = FakeMessage(text="/start", from_user=FakeUser(id=100))
        assert self._filter(m, True, 100) is False

    def test_not_waiting(self):
        m = FakeMessage(text="Some text", from_user=FakeUser(id=100))
        assert self._filter(m, False, 100) is False

    def test_not_admin(self):
        m = FakeMessage(text="Some text", from_user=FakeUser(id=999))
        assert self._filter(m, True, 100) is False

    def test_none_text_no_crash(self):
        """Photo from admin while waiting_theme — must not crash."""
        m = FakeMessage(text=None, from_user=FakeUser(id=100))
        assert self._filter(m, True, 100) is False

    def test_empty_text(self):
        m = FakeMessage(text="", from_user=FakeUser(id=100))
        assert self._filter(m, True, 100) is False


# ============================================================
# Callback data parsing safety
# ============================================================

class TestCallbackDataParsing:
    """Malformed callback data must not crash handlers."""

    def test_valid_vote_data(self):
        data = "vote:keep:42"
        parts = data.split(":")
        assert len(parts) == 3
        from app.bot.core import safe_int
        assert safe_int(parts[2]) == 42

    def test_missing_id(self):
        data = "vote:keep:"
        parts = data.split(":")
        from app.bot.core import safe_int
        assert safe_int(parts[2]) is None  # empty string

    def test_non_numeric_id(self):
        data = "vote:keep:abc"
        parts = data.split(":")
        from app.bot.core import safe_int
        assert safe_int(parts[2]) is None

    def test_too_few_parts(self):
        data = "vote:keep"
        parts = data.split(":")
        assert len(parts) != 3  # handler should reject

    def test_tampered_approve_data(self):
        data = "approve:not_a_number"
        from app.bot.core import safe_int
        assert safe_int(data.split(":")[1]) is None

    def test_recap_page_both_parts(self):
        data = "recap_page:91:2"
        parts = data.split(":")
        from app.bot.core import safe_int
        assert safe_int(parts[1]) == 91
        assert safe_int(parts[2]) == 2

    def test_recap_page_malformed(self):
        data = "recap_page:abc:def"
        parts = data.split(":")
        from app.bot.core import safe_int
        assert safe_int(parts[1]) is None
        assert safe_int(parts[2]) is None

    def test_history_offset(self):
        data = "history:10"
        from app.bot.core import safe_int
        result = safe_int(data.split(":")[1]) or 0
        assert result == 10

    def test_history_offset_malformed(self):
        data = "history:xyz"
        from app.bot.core import safe_int
        result = safe_int(data.split(":")[1]) or 0
        assert result == 0  # fallback to 0
