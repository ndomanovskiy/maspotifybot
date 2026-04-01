"""Shared fixtures for MaSpotifyBot tests."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- Fake asyncpg primitives ---

class FakeConnection:
    """In-memory fake for asyncpg.Connection backed by a shared store."""

    def __init__(self, store: "FakeStore"):
        self._store = store

    async def fetch(self, query: str, *args):
        return self._store.handle_fetch(query, args)

    async def fetchval(self, query: str, *args):
        return self._store.handle_fetchval(query, args)

    async def fetchrow(self, query: str, *args):
        return self._store.handle_fetchrow(query, args)

    async def execute(self, query: str, *args):
        return self._store.handle_execute(query, args)


class FakePoolContext:
    def __init__(self, conn: FakeConnection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


class FakePool:
    """Fake asyncpg.Pool that returns FakeConnection."""

    def __init__(self, store: "FakeStore"):
        self._store = store

    def acquire(self):
        return FakePoolContext(FakeConnection(self._store))


class FakeRecord(dict):
    """Dict subclass that supports attribute-style access like asyncpg.Record."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


@dataclass
class FakeStore:
    """In-memory data store backing FakePool.

    Tables:
      playlists: list[dict]
      playlist_tracks: list[dict] (auto-increment id)
      users: list[dict]
    """

    playlists: list[dict] = field(default_factory=list)
    playlist_tracks: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)
    _next_pt_id: int = 1

    # --- helpers ---

    def add_playlist(self, *, id: int, spotify_id: str, name: str, status: str = "upcoming",
                     is_thematic: bool = False, number: int | None = None) -> dict:
        rec = {"id": id, "spotify_id": spotify_id, "name": name, "status": status,
               "is_thematic": is_thematic, "number": number}
        self.playlists.append(rec)
        return rec

    def add_track(self, *, playlist_id: int, spotify_track_id: str, title: str = "",
                  artist: str = "", isrc: str | None = None, ai_facts: str | None = None,
                  added_by_spotify_id: str | None = None) -> dict:
        rec = {"id": self._next_pt_id, "playlist_id": playlist_id,
               "spotify_track_id": spotify_track_id, "title": title, "artist": artist,
               "isrc": isrc, "ai_facts": ai_facts, "added_by_spotify_id": added_by_spotify_id,
               "added_at": None}
        self._next_pt_id += 1
        self.playlist_tracks.append(rec)
        return rec

    def add_user(self, *, telegram_id: int, spotify_id: str):
        self.users.append({"telegram_id": telegram_id, "spotify_id": spotify_id})

    def get_track(self, playlist_id: int, spotify_track_id: str) -> dict | None:
        for t in self.playlist_tracks:
            if t["playlist_id"] == playlist_id and t["spotify_track_id"] == spotify_track_id:
                return t
        return None

    # --- query handlers ---

    def handle_fetch(self, query: str, args: tuple) -> list[FakeRecord]:
        q = query.strip().lower()

        if "from playlists" in q and "status in" in q:
            return [FakeRecord(p) for p in self.playlists if p["status"] in ("active", "upcoming")]

        if "from playlist_tracks" in q and "playlist_id = $1" in q and "ai_facts" not in q:
            pid = args[0]
            return [FakeRecord({"spotify_track_id": t["spotify_track_id"]})
                    for t in self.playlist_tracks if t["playlist_id"] == pid]

        if "from playlist_tracks" in q and "ai_facts is null" in q:
            return [FakeRecord({"id": t["id"], "spotify_track_id": t["spotify_track_id"],
                                "title": t["title"], "artist": t["artist"]})
                    for t in self.playlist_tracks
                    if t["ai_facts"] is None
                    and any(p["id"] == t["playlist_id"] and p["status"] == "upcoming"
                            for p in self.playlists)]

        return []

    def handle_fetchval(self, query: str, args: tuple):
        q = query.strip().lower()

        if "select exists" in q and "users" in q:
            tid = args[0]
            return any(u["telegram_id"] == tid for u in self.users)

        if "select exists" in q and "playlist_tracks" in q:
            pid, tid = args[0], args[1]
            return any(t["playlist_id"] == pid and t["spotify_track_id"] == tid
                       for t in self.playlist_tracks)

        if "is_thematic" in q:
            pid = args[0]
            for p in self.playlists:
                if p["id"] == pid:
                    return p["is_thematic"]
            return None

        if "ai_facts" in q and "spotify_track_id" in q:
            tid = args[0]
            for t in self.playlist_tracks:
                if t["spotify_track_id"] == tid and t["ai_facts"] is not None:
                    return t["ai_facts"]
            return None

        return None

    def handle_fetchrow(self, query: str, args: tuple):
        q = query.strip().lower()

        if "from users" in q and "spotify_id" in q:
            sid = args[0]
            for u in self.users:
                if u["spotify_id"] == sid:
                    return FakeRecord(u)
            return None

        return None

    def handle_execute(self, query: str, args: tuple):
        q = query.strip().lower()

        if "insert into playlist_tracks" in q:
            pid, tid = args[0], args[1]
            # ON CONFLICT DO NOTHING
            if self.get_track(pid, tid):
                return
            isrc = args[2]
            title = args[3]
            artist = args[4]
            added_by = args[5]
            added_at = args[6]
            self.playlist_tracks.append({
                "id": self._next_pt_id,
                "playlist_id": pid,
                "spotify_track_id": tid,
                "isrc": isrc,
                "title": title,
                "artist": artist,
                "added_by_spotify_id": added_by,
                "added_at": added_at,
                "ai_facts": None,
            })
            self._next_pt_id += 1

        elif "update playlist_tracks set ai_facts" in q:
            facts = args[0]
            pt_id = args[1]
            for t in self.playlist_tracks:
                if t["id"] == pt_id:
                    t["ai_facts"] = facts
                    break

        elif "delete from playlist_tracks" in q:
            pid, tid = args[0], args[1]
            self.playlist_tracks = [
                t for t in self.playlist_tracks
                if not (t["playlist_id"] == pid and t["spotify_track_id"] == tid)
            ]


# --- Spotify mock helpers ---

@dataclass
class FakeArtist:
    name: str


@dataclass
class FakeAlbum:
    name: str
    images: list = field(default_factory=list)


@dataclass
class FakeExternalIds:
    isrc: str | None = None


@dataclass
class FakeTrack:
    id: str
    name: str
    artists: list[FakeArtist] = field(default_factory=list)
    album: FakeAlbum = field(default_factory=lambda: FakeAlbum(name=""))
    external_ids: FakeExternalIds | None = None
    duration_ms: int = 200000


@dataclass
class FakeAddedBy:
    id: str


@dataclass
class FakePlaylistItem:
    track: FakeTrack | None
    added_by: FakeAddedBy | None = None
    added_at: str | None = None


@dataclass
class FakePlaylistItems:
    items: list[FakePlaylistItem]
    total: int = 0

    def __post_init__(self):
        if self.total == 0:
            self.total = len(self.items)


# --- Fixtures ---

@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def fake_pool(store):
    return FakePool(store)


def make_track(track_id: str, name: str = "Track", artist: str = "Artist",
               album: str = "Album", isrc: str | None = None) -> FakeTrack:
    return FakeTrack(
        id=track_id,
        name=name,
        artists=[FakeArtist(name=artist)],
        album=FakeAlbum(name=album),
        external_ids=FakeExternalIds(isrc=isrc) if isrc else None,
    )


def make_item(track: FakeTrack, added_by: str | None = None) -> FakePlaylistItem:
    return FakePlaylistItem(
        track=track,
        added_by=FakeAddedBy(id=added_by) if added_by else None,
    )
