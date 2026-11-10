"""Session recap blocks: participant names and secrets are text, never markup.

A name with '<' used to break the stats block, so nobody got the recap and /recap kept failing.
"""

from app.services.admin_commands import _easter_egg_message, _user_stats_lines


def test_recap_stats_escape_participant_names():
    lines = _user_stats_lines({"<Bob>": {"kept": 1, "total": 2}, "Ann": {"kept": 3, "total": 3}})

    assert lines == ["   Ann — 3 из 3", "   &lt;Bob&gt; — 1 из 2"]


def test_easter_egg_without_ai_text_escapes_name_and_secret():
    text = _easter_egg_message("<Bob>", "мои треки <3", "")

    assert text == "🥚 <b>Пасхалка от &lt;Bob&gt;</b>\n\n<i>мои треки &lt;3</i>"


def test_easter_egg_ai_text_stays_html():
    assert _easter_egg_message("Bob", "s", "<b>угадали</b>").endswith("\n\n<b>угадали</b>")
