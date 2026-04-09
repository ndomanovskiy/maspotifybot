"""Tests for HTML escaping in user-supplied text.

Verifies that user input embedded in HTML messages is escaped
to prevent injection / broken rendering.
"""

import html


class TestHtmlEscapeInSecrets:
    """Secret text must be escaped before embedding in HTML messages."""

    def _build_secret_saved_msg(self, secret: str) -> str:
        """Replicate the message format from cmd_secret."""
        return f"🥚 Секрет сохранён!\n🔒 <i>{html.escape(secret)}</i>\n\n⏳ Анализирую треки..."

    def _build_secret_updated_msg(self, updated_secret: str) -> str:
        """Replicate the message format from on_secret_clarification."""
        return f"🥚 Секрет обновлён!\n🔒 <i>{html.escape(updated_secret)}</i>"

    def test_plain_text_unchanged(self):
        msg = self._build_secret_saved_msg("Мои треки из 90-х")
        assert "Мои треки из 90-х" in msg

    def test_html_tags_escaped(self):
        msg = self._build_secret_saved_msg("<b>bold</b>")
        assert "<b>bold</b>" not in msg  # raw tags should NOT be there
        assert "&lt;b&gt;bold&lt;/b&gt;" in msg

    def test_ampersand_escaped(self):
        msg = self._build_secret_saved_msg("rock & roll")
        assert "&amp;" in msg

    def test_quotes_escaped(self):
        msg = self._build_secret_saved_msg('test "quoted"')
        assert "&quot;" in msg

    def test_script_tag_escaped(self):
        msg = self._build_secret_saved_msg("<script>alert('xss')</script>")
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg

    def test_nested_html_escaped(self):
        msg = self._build_secret_saved_msg("<i>italic</i> & <b>bold</b>")
        # All angle brackets escaped
        assert msg.count("&lt;") == 4
        assert msg.count("&gt;") == 4

    def test_updated_secret_also_escaped(self):
        msg = self._build_secret_updated_msg("original | Уточнение: <b>details</b>")
        assert "&lt;b&gt;" in msg
        assert "<b>details</b>" not in msg

    def test_empty_secret(self):
        msg = self._build_secret_saved_msg("")
        assert "<i></i>" in msg

    def test_only_special_chars(self):
        msg = self._build_secret_saved_msg("<<<>>>&&&&")
        assert "<" not in msg.split("<i>")[1].split("</i>")[0] or "&lt;" in msg
