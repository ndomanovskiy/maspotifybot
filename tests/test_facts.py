"""Hand-written track facts: only HTML Telegram can show goes into the database."""

import asyncio
import json

import pytest

from app.services.facts import facts_html_problems, pending_tracks, save_facts

GOOD = "🎤 <b>Simon &amp; Garfunkel</b> записали трек в 1970\n📈 <i>№1</i> в <code>Billboard</code> &#x27;70"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def problems(text):
    return " | ".join(facts_html_problems(text))


def test_plain_telegram_html_is_accepted():
    assert facts_html_problems(GOOD) == []


def test_a_link_or_other_tag_is_rejected():
    assert "not allowed" in problems('see <a href="x">here</a>')


def test_crossed_tags_are_rejected():
    found = problems("<i>«Maniac» (1980)</b>")
    assert "closes a tag that is not open" in found and "not closed on this line: <i>" in found
    assert "before the inner <i>" in problems("<b><i>x</b></i>")


def test_an_entity_without_its_semicolon_gets_a_precise_hint():
    assert "end it with ';'" in problems("Tom &amp Jerry")


def test_an_unclosed_tag_is_rejected():
    assert "not closed" in problems("<b>bold forever")


def test_a_tag_must_close_on_its_own_line():
    # track cards drop whole lines to fit, which would leave the tag open
    assert "not closed" in problems("🎤 <i>line one\nline two</i>")


def test_nothing_may_be_nested_inside_code():
    assert "inside <code>" in problems("<code><b>x</b></code>")


def test_a_bare_ampersand_or_angle_bracket_is_rejected():
    assert "&amp;" in problems("Simon & Garfunkel")
    assert "write &lt;" in problems("I <3 this")
    assert "write &gt;" in problems("3 > 2")
    assert "write &lt;" in problems("5 < 10 and 20 > 15")


class _Pool:
    """create_pool stand-in: the shared fake pool plus the close() the CLI calls."""

    def __init__(self, pool):
        self._pool = pool

    def acquire(self):
        return self._pool.acquire()

    async def close(self):
        pass


class _UnreadableStdin:
    def read(self, *args):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def cli(monkeypatch, fake_pool, args, stdin=""):
    import io

    from app.services import facts

    async def create_pool(*a, **kw):
        return _Pool(fake_pool)

    monkeypatch.setattr(facts.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr("sys.stdin", stdin if not isinstance(stdin, str) else io.StringIO(stdin))
    return run(facts._main(args))


def test_save_command_exits_3_when_something_was_not_saved(store, fake_pool, monkeypatch):
    add_playlist_with_tracks(store)

    assert cli(monkeypatch, fake_pool, ["save"], json.dumps({"t1": "🎤 ok", "t3": "<i>x</b>"})) == 3
    assert store.get_track(1, "t1")["ai_facts"] == "🎤 ok"


def test_save_command_exits_0_when_everything_was_saved(store, fake_pool, monkeypatch):
    add_playlist_with_tracks(store)

    assert cli(monkeypatch, fake_pool, ["save"], json.dumps({"t1": "🎤 ok"})) == 0


def test_bad_input_exits_2(fake_pool, monkeypatch):
    assert cli(monkeypatch, fake_pool, ["save"], "not json") == 2
    assert cli(monkeypatch, fake_pool, ["save"], _UnreadableStdin()) == 2  # a file saved in cp1251
    assert cli(monkeypatch, fake_pool, ["pending", "²"]) == 2
    assert cli(monkeypatch, fake_pool, ["save", "extra"]) == 2


def test_pending_command_prints_the_tracks(store, fake_pool, monkeypatch, capsys):
    add_playlist_with_tracks(store)

    assert cli(monkeypatch, fake_pool, ["pending", "110"]) == 0
    assert '"spotify_track_id": "t1"' in capsys.readouterr().out


def test_pending_for_a_playlist_that_does_not_exist_exits_2(store, fake_pool, monkeypatch, capsys):
    add_playlist_with_tracks(store)

    assert run(pending_tracks(fake_pool, 1088)) is None
    assert cli(monkeypatch, fake_pool, ["pending", "1088"]) == 2
    assert "TURDOM#1088" in capsys.readouterr().err


def test_pending_takes_the_newest_playlist_with_that_number(store, fake_pool):
    add_playlist_with_tracks(store)
    store.add_playlist(id=5, spotify_id="sp5", name="TURDOM#110 again", status="upcoming", number=110)
    store.add_track(playlist_id=5, spotify_track_id="t5", title="Five", artist="E")

    assert [t["spotify_track_id"] for t in run(pending_tracks(fake_pool, 110))] == ["t5"]


def test_save_command_checks_the_json_shape_before_touching_the_database(monkeypatch, capsys):
    import io

    from app.services import facts

    def no_database(*args, **kwargs):
        raise AssertionError("the database must not be opened for malformed input")

    monkeypatch.setattr(facts.asyncpg, "create_pool", no_database)
    monkeypatch.setattr("sys.stdin", io.StringIO('["t1", "facts"]'))

    assert run(facts._main(["save"])) == 2
    assert "JSON object" in capsys.readouterr().err


def test_empty_or_too_long_facts_are_rejected():
    assert "empty" in problems("   ")
    assert "longer than 750" in problems("а" * 751)
    assert facts_html_problems("  " + "а" * 750 + "\n") == []


def add_playlist_with_tracks(store):
    store.add_playlist(id=1, spotify_id="sp1", name="TURDOM#110", status="upcoming", number=110)
    store.add_track(playlist_id=1, spotify_track_id="t1", title="One", artist="A")
    store.add_track(playlist_id=1, spotify_track_id="t2", title="Two", artist="B", ai_facts="old facts")
    store.add_track(playlist_id=1, spotify_track_id="t4", title="Four", artist="D")
    store.add_playlist(id=2, spotify_id="sp2", name="TURDOM#109", status="listened", number=109)
    store.add_track(playlist_id=2, spotify_track_id="t3", title="Three", artist="C")


def test_pending_lists_only_tracks_of_that_playlist_without_facts(store, fake_pool):
    add_playlist_with_tracks(store)

    pending = run(pending_tracks(fake_pool, 110))

    assert [t["spotify_track_id"] for t in pending] == ["t1", "t4"]


def test_save_writes_good_facts_and_refuses_broken_ones(store, fake_pool):
    add_playlist_with_tracks(store)

    result = run(save_facts(fake_pool, {"t1": GOOD, "t3": "<i>broken</b>", "t4": None, "nope": GOOD}))

    assert result["saved"] == ["t1"]
    assert sorted(result["rejected"]) == ["t3", "t4"]
    assert result["unknown"] == ["nope"]
    assert store.get_track(1, "t1")["ai_facts"] == GOOD
    assert store.get_track(2, "t3")["ai_facts"] is None


def test_save_refuses_anything_but_an_object(fake_pool):
    with pytest.raises(TypeError):
        run(save_facts(fake_pool, ["t1", GOOD]))
