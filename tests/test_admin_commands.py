"""Tests for admin commands: /distribute, /recap, /close_playlist, /create_next, /dbinfo."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import FakeRecord


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- Extended FakeStore for admin commands ---

class AdminFakeStore:
    def __init__(self):
        self.playlists: list[dict] = []
        self.sessions: list[dict] = []
        self.session_tracks: list[dict] = []
        self.session_participants: list[dict] = []
        self.users: list[dict] = []
        self.action_log: list[dict] = []
        self._next_id = 100

    def _id(self):
        self._next_id += 1
        return self._next_id

    def add_playlist(self, *, number, spotify_id, name, status="upcoming"):
        rec = {"id": self._id(), "spotify_id": spotify_id, "name": name,
               "number": number, "status": status, "invite_url": None}
        self.playlists.append(rec)
        return rec

    def add_session(self, *, playlist_spotify_id, playlist_name,
                    status="ended", ended_at=None, recap_text=None, distributed_at=None):
        rec = {"id": self._id(), "playlist_spotify_id": playlist_spotify_id,
               "playlist_name": playlist_name, "status": status,
               "started_at": datetime(2026, 4, 1, 11, 0, tzinfo=timezone.utc),
               "ended_at": ended_at or datetime(2026, 4, 1, 12, 52, tzinfo=timezone.utc),
               "recap_text": recap_text, "distributed_at": distributed_at}
        self.sessions.append(rec)
        return rec

    def add_session_track(self, *, session_id, spotify_track_id, title, artist,
                          vote_result="keep", added_by_spotify_id=None):
        rec = {"id": self._id(), "session_id": session_id,
               "spotify_track_id": spotify_track_id, "title": title, "artist": artist,
               "vote_result": vote_result, "added_by_spotify_id": added_by_spotify_id}
        self.session_tracks.append(rec)
        return rec

    def add_participant(self, *, session_id, telegram_id, active=True):
        rec = {"session_id": session_id, "telegram_id": telegram_id, "active": active}
        self.session_participants.append(rec)
        return rec

    def add_user(self, *, telegram_id, telegram_name, telegram_username="", spotify_id=""):
        rec = {"telegram_id": telegram_id, "telegram_name": telegram_name,
               "telegram_username": telegram_username, "spotify_id": spotify_id}
        self.users.append(rec)
        return rec

    def get_session(self, session_id):
        return next((s for s in self.sessions if s["id"] == session_id), None)

    def get_playlist_by_number(self, number):
        return next((p for p in self.playlists if p["number"] == number), None)


class AdminFakeConnection:
    def __init__(self, store):
        self._store = store

    async def fetch(self, query, *args):
        q = query.strip().lower()
        s = self._store

        if "count(*)" in q and "session_tracks" in q and "session_id" in q:
            sid = args[0]
            tracks = [t for t in s.session_tracks if t["session_id"] == sid]
            return [FakeRecord({"total": len(tracks),
                                "kept": sum(1 for t in tracks if t["vote_result"] == "keep"),
                                "dropped": sum(1 for t in tracks if t["vote_result"] == "drop")})]

        if "st.title" in q and "session_tracks st" in q:
            sid = args[0]
            result = []
            for t in s.session_tracks:
                if t["session_id"] == sid:
                    added_by = t.get("added_by_spotify_id", "?")
                    for u in s.users:
                        if u["spotify_id"] == added_by:
                            added_by = f"@{u['telegram_username']}" if u["telegram_username"] else u["telegram_name"]
                            break
                    result.append(FakeRecord({**t, "added_by": added_by}))
            return result

        if "telegram_name" in q and "session_participants" in q:
            sid = args[0]
            result = []
            for sp in s.session_participants:
                if sp["session_id"] == sid:
                    for u in s.users:
                        if u["telegram_id"] == sp["telegram_id"]:
                            result.append(FakeRecord({"telegram_name": u["telegram_name"]}))
            return result

        if "telegram_id" in q and "session_participants" in q:
            sid = args[0]
            return [FakeRecord({"telegram_id": sp["telegram_id"]})
                    for sp in s.session_participants if sp["session_id"] == sid and sp["active"]]

        if "from playlists" in q and "status in" in q:
            return [FakeRecord(p) for p in s.playlists if p["status"] in ("active", "upcoming")]

        if "s.id" in q and "sessions s" in q:
            result = []
            for sess in sorted(s.sessions, key=lambda x: x["id"], reverse=True)[:3]:
                tracks = [t for t in s.session_tracks if t["session_id"] == sess["id"]]
                result.append(FakeRecord({
                    "id": sess["id"], "playlist_name": sess["playlist_name"],
                    "status": sess["status"], "started_at": sess["started_at"],
                    "ended_at": sess["ended_at"], "distributed_at": sess["distributed_at"],
                    "has_recap": sess["recap_text"] is not None,
                    "total_tracks": len(tracks),
                    "kept": sum(1 for t in tracks if t["vote_result"] == "keep"),
                    "dropped": sum(1 for t in tracks if t["vote_result"] == "drop"),
                }))
            return result

        if "action_log" in q:
            return [FakeRecord({"action": a["action"], "turdom_number": a.get("turdom_number"),
                                "status": a["status"], "created_at": datetime.now(timezone.utc)})
                    for a in s.action_log[-5:]]

        return []

    async def fetchval(self, query, *args):
        q = query.strip().lower()
        s = self._store

        if "distributed_at" in q and "sessions" in q:
            sess = s.get_session(args[0])
            return sess["distributed_at"] if sess else None

        if "recap_text" in q and "sessions" in q:
            sess = s.get_session(args[0])
            return sess["recap_text"] if sess else None

        if "count(*)" in q and "sessions" in q:
            return sum(1 for sess in s.sessions if sess["playlist_spotify_id"] == args[0])

        if "count(*)" in q and "users" in q:
            return len(s.users)

        if "max(number)" in q:
            numbers = [p["number"] for p in s.playlists if p["number"] is not None]
            return max(numbers) if numbers else None

        return None

    async def fetchrow(self, query, *args):
        q = query.strip().lower()
        s = self._store

        if "count(*)" in q and "session_tracks" in q:
            sid = args[0]
            tracks = [t for t in s.session_tracks if t["session_id"] == sid]
            return FakeRecord({"total": len(tracks),
                               "kept": sum(1 for t in tracks if t["vote_result"] == "keep"),
                               "dropped": sum(1 for t in tracks if t["vote_result"] == "drop")})

        if "from playlists" in q and "number = $1" in q:
            p = s.get_playlist_by_number(args[0])
            return FakeRecord(p) if p else None

        if "from sessions" in q and "playlist_spotify_id" in q:
            for sess in reversed(s.sessions):
                if sess["playlist_spotify_id"] == args[0]:
                    return FakeRecord(sess)
            return None

        if "from playlists" in q and "status in" in q:
            for p in reversed(s.playlists):
                if p["status"] in ("active", "upcoming"):
                    return FakeRecord(p)
            return None

        if "from sessions" in q and "status = 'ended'" in q:
            for sess in reversed(s.sessions):
                if sess["status"] == "ended":
                    return FakeRecord(sess)
            return None

        return None

    async def execute(self, query, *args):
        q = query.strip().lower()
        s = self._store

        if "insert into action_log" in q:
            s.action_log.append({
                "action": args[0], "turdom_number": args[1], "session_id": args[2],
                "playlist_id": args[3], "triggered_by": args[4],
                "params": args[5], "result": args[6], "status": args[7],
            })
        elif "update sessions set distributed_at" in q:
            sess = s.get_session(args[0])
            if sess:
                sess["distributed_at"] = datetime.now(timezone.utc)
        elif "update sessions set recap_text" in q:
            sess = s.get_session(args[1])
            if sess:
                sess["recap_text"] = args[0]
        elif "update playlists set status = 'listened'" in q and "name = $1" in q:
            for p in s.playlists:
                if p["id"] == args[1]:
                    p["status"] = "listened"
                    p["name"] = args[0]
                    break


class AdminFakePoolCtx:
    def __init__(self, conn):
        self._conn = conn
    async def __aenter__(self):
        return self._conn
    async def __aexit__(self, *args):
        pass


class AdminFakePool:
    def __init__(self, store):
        self._store = store
    def acquire(self):
        return AdminFakePoolCtx(AdminFakeConnection(self._store))


@pytest.fixture
def admin_store():
    return AdminFakeStore()

@pytest.fixture
def admin_pool(admin_store):
    return AdminFakePool(admin_store)


# ---------------------------------------------------------------------------
# resolve_turdom
# ---------------------------------------------------------------------------

class TestResolveTurdom:
    def test_resolve_existing(self, admin_store, admin_pool):
        from app.services.admin_commands import resolve_turdom
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91 01/04/2026")
        sess = admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91")

        result = run(resolve_turdom(admin_pool, 91))
        assert result is not None
        assert result["playlist_spotify_id"] == "sp_91"
        assert result["session_id"] == sess["id"]
        assert result["session_status"] == "ended"

    def test_resolve_not_found(self, admin_pool):
        from app.services.admin_commands import resolve_turdom
        assert run(resolve_turdom(admin_pool, 999)) is None

    def test_resolve_no_session(self, admin_store, admin_pool):
        from app.services.admin_commands import resolve_turdom
        admin_store.add_playlist(number=92, spotify_id="sp_92", name="TURDOM#92")

        result = run(resolve_turdom(admin_pool, 92))
        assert result is not None
        assert result["session_id"] is None


# ---------------------------------------------------------------------------
# /distribute
# ---------------------------------------------------------------------------

class TestDistribute:
    def test_not_found(self, admin_pool):
        from app.services.admin_commands import cmd_distribute
        result = run(cmd_distribute(admin_pool, 999))
        assert result["status"] == "error"
        assert "не найден" in result["message"]

    def test_no_session(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_distribute
        admin_store.add_playlist(number=92, spotify_id="sp_92", name="TURDOM#92")
        result = run(cmd_distribute(admin_pool, 92))
        assert result["status"] == "error"
        assert "нет сессии" in result["message"]

    def test_active_session(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_distribute
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91")
        admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91", status="active")
        result = run(cmd_distribute(admin_pool, 91))
        assert result["status"] == "error"
        assert "ещё активна" in result["message"]

    def test_already_done(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_distribute
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91")
        admin_store.add_session(
            playlist_spotify_id="sp_91", playlist_name="TURDOM#91",
            distributed_at=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
        )
        result = run(cmd_distribute(admin_pool, 91))
        assert result["status"] == "already_done"
        assert "уже раскиданы" in result["message"]

    @patch("app.services.admin_commands.distribute_session_tracks")
    def test_success(self, mock_dist, admin_store, admin_pool):
        from app.services.admin_commands import cmd_distribute

        async def fake_dist(*a, **kw):
            return {"distributed": 15, "skipped": 3}
        mock_dist.side_effect = fake_dist

        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91")
        sess = admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91")

        result = run(cmd_distribute(admin_pool, 91, triggered_by=123))
        assert result["status"] == "ok"
        assert result["distributed"] == 15
        assert "15 треков" in result["message"]
        assert len(admin_store.action_log) == 1
        assert admin_store.action_log[0]["action"] == "distribute"
        assert sess["distributed_at"] is not None


# ---------------------------------------------------------------------------
# /recap
# ---------------------------------------------------------------------------

class TestRecap:
    def test_not_found(self, admin_pool):
        from app.services.admin_commands import cmd_recap
        result = run(cmd_recap(admin_pool, 999))
        assert result["status"] == "error"

    def test_returns_saved(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_recap
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91")
        admin_store.add_session(
            playlist_spotify_id="sp_91", playlist_name="TURDOM#91",
            recap_text="Saved recap text here",
        )
        result = run(cmd_recap(admin_pool, 91))
        assert result["status"] == "ok"
        assert result["has_saved"] is True
        assert "Saved recap text here" in result["message"]

    @patch("app.services.admin_commands.generate_session_recap_blocks")
    def test_generates_new(self, mock_gen, admin_store, admin_pool):
        from app.services.admin_commands import cmd_recap

        async def fake_gen(*a, **kw):
            return {"genres": "Generated recap"}
        mock_gen.side_effect = fake_gen

        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91")
        sess = admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91")
        admin_store.add_session_track(
            session_id=sess["id"], spotify_track_id="t1",
            title="Song", artist="Artist", vote_result="keep",
        )
        result = run(cmd_recap(admin_pool, 91))
        assert result["status"] == "ok"
        assert result["has_saved"] is False
        assert "TURDOM#91" in result["message"]
        assert "Generated recap" in sess["recap_text"]


# ---------------------------------------------------------------------------
# /close_playlist
# ---------------------------------------------------------------------------

class TestClosePlaylist:
    def test_not_found(self, admin_pool):
        from app.services.admin_commands import cmd_close_playlist
        result = run(cmd_close_playlist(admin_pool, 999))
        assert result["status"] == "error"

    def test_already_closed(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_close_playlist
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91", status="listened")
        result = run(cmd_close_playlist(admin_pool, 91))
        assert result["status"] == "error"
        assert "уже закрыт" in result["message"]

    @patch("app.services.admin_commands.get_spotify")
    def test_updates_date(self, mock_sp, admin_store, admin_pool):
        from app.services.admin_commands import cmd_close_playlist

        mock_spotify = AsyncMock()
        mock_sp.return_value = mock_spotify

        pl = admin_store.add_playlist(number=91, spotify_id="sp_91",
                                       name="TURDOM#91 18/03/2026", status="upcoming")
        admin_store.add_session(
            playlist_spotify_id="sp_91", playlist_name="TURDOM#91 18/03/2026",
            ended_at=datetime(2026, 4, 1, 12, 52, tzinfo=timezone.utc),
        )
        result = run(cmd_close_playlist(admin_pool, 91))
        assert result["status"] == "ok"
        assert "01/04/2026" in result["message"]
        assert pl["status"] == "listened"
        assert "01/04/2026" in pl["name"]
        mock_spotify.playlist_change_details.assert_called_once()


# ---------------------------------------------------------------------------
# /create_next
# ---------------------------------------------------------------------------

class TestCreateNext:
    def test_blocked_by_open_playlist(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_create_next
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91", status="upcoming")
        result = run(cmd_create_next(admin_pool))
        assert result["status"] == "blocked"
        assert "Есть открытый плейлист" in result["message"]
        assert "/close_playlist 91" in result["message"]

    @patch("app.services.admin_commands.create_next_playlist")
    def test_success(self, mock_create, admin_store, admin_pool):
        from app.services.admin_commands import cmd_create_next

        async def fake_create(*a, **kw):
            return {"name": "TURDOM#92 09/04/2026", "number": 92,
                    "url": "https://open.spotify.com/playlist/sp_92", "spotify_id": "sp_92"}
        mock_create.side_effect = fake_create

        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91", status="listened")
        result = run(cmd_create_next(admin_pool))
        assert result["status"] == "ok"
        assert "TURDOM#92" in result["message"]


# ---------------------------------------------------------------------------
# /dbinfo
# ---------------------------------------------------------------------------

class TestDbinfo:
    def test_output(self, admin_store, admin_pool):
        from app.services.admin_commands import cmd_dbinfo
        admin_store.add_playlist(number=91, spotify_id="sp_91", name="TURDOM#91", status="listened")
        sess = admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91")
        admin_store.add_session_track(
            session_id=sess["id"], spotify_track_id="t1",
            title="Song", artist="Artist", vote_result="keep",
        )
        admin_store.add_user(telegram_id=123, telegram_name="Nikita")

        text = run(cmd_dbinfo(admin_pool))
        assert "Database Info" in text
        assert "TURDOM#91" in text
        assert "Юзеров" in text


# ---------------------------------------------------------------------------
# check_duplicate_session
# ---------------------------------------------------------------------------

class TestDuplicateSession:
    def test_no_duplicate(self, admin_pool):
        from app.services.admin_commands import check_duplicate_session
        assert run(check_duplicate_session(admin_pool, "sp_new")) is False

    def test_has_duplicate(self, admin_store, admin_pool):
        from app.services.admin_commands import check_duplicate_session
        admin_store.add_session(playlist_spotify_id="sp_91", playlist_name="TURDOM#91")
        assert run(check_duplicate_session(admin_pool, "sp_91")) is True


# ---------------------------------------------------------------------------
# to_msk
# ---------------------------------------------------------------------------

class TestToMsk:
    def test_utc_to_msk(self):
        from app.services.admin_commands import to_msk
        dt = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        result = to_msk(dt)
        assert "15:00 MSK" in result
        assert "01.04.2026" in result

    def test_none(self):
        from app.services.admin_commands import to_msk
        assert to_msk(None) == "—"

    def test_naive_treated_as_utc(self):
        from app.services.admin_commands import to_msk
        dt = datetime(2026, 4, 1, 12, 0)
        assert "15:00 MSK" in to_msk(dt)
