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
      tracks: list[dict] (auto-increment id) — canonical track data
      playlist_tracks: list[dict] (auto-increment id) — playlist↔track links
      users: list[dict]
    """

    playlists: list[dict] = field(default_factory=list)
    tracks: list[dict] = field(default_factory=list)
    playlist_tracks: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)
    _next_track_id: int = 1
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
        """Add a track to both `tracks` and `playlist_tracks` tables.

        Returns a composite dict that merges track-level and link-level fields,
        keeping backward compat with existing tests that read title/artist/ai_facts
        from the returned dict and also expect store.get_track() to work.
        """
        # Upsert into tracks table
        track_rec = None
        for t in self.tracks:
            if t["spotify_track_id"] == spotify_track_id:
                track_rec = t
                break
        if track_rec is None:
            track_rec = {
                "id": self._next_track_id,
                "spotify_track_id": spotify_track_id,
                "title": title,
                "artist": artist,
                "isrc": isrc,
                "ai_facts": ai_facts,
                "normalized_title": None,
                "normalized_artist": None,
            }
            self._next_track_id += 1
            self.tracks.append(track_rec)
        else:
            # Update existing track fields
            track_rec["title"] = title
            track_rec["artist"] = artist
            if isrc is not None:
                track_rec["isrc"] = isrc
            if ai_facts is not None:
                track_rec["ai_facts"] = ai_facts

        # Insert into playlist_tracks link table
        pt_rec = {
            "id": self._next_pt_id,
            "playlist_id": playlist_id,
            "track_id": track_rec["id"],
            "spotify_track_id": spotify_track_id,
            "added_by_spotify_id": added_by_spotify_id,
            "added_at": None,
        }
        self._next_pt_id += 1
        self.playlist_tracks.append(pt_rec)

        # Return the track_rec so tests that mutate it (e.g. checking ai_facts) work
        return track_rec

    def add_user(self, *, telegram_id: int, spotify_id: str,
                 telegram_name: str = "User", telegram_username: str | None = None):
        self.users.append({
            "telegram_id": telegram_id, "spotify_id": spotify_id,
            "telegram_name": telegram_name, "telegram_username": telegram_username,
        })

    def get_track(self, playlist_id: int, spotify_track_id: str) -> dict | None:
        """Find a track by playlist_id + spotify_track_id.

        Returns the tracks-table dict (with title, artist, ai_facts) if found.
        """
        for pt in self.playlist_tracks:
            if pt["playlist_id"] == playlist_id and pt["spotify_track_id"] == spotify_track_id:
                # Return the corresponding tracks entry
                for t in self.tracks:
                    if t["id"] == pt["track_id"]:
                        return t
                return None
        return None

    def _get_track_by_spotify_id(self, spotify_track_id: str) -> dict | None:
        for t in self.tracks:
            if t["spotify_track_id"] == spotify_track_id:
                return t
        return None

    # --- query handlers ---

    def handle_fetch(self, query: str, args: tuple) -> list[FakeRecord]:
        q = query.strip().lower()

        if "from playlists" in q and "status in" in q:
            return [FakeRecord(p) for p in self.playlists if p["status"] in ("active", "upcoming")]

        if "from playlist_tracks" in q and "playlist_id = $1" in q and "ai_facts" not in q:
            pid = args[0]
            return [FakeRecord({"spotify_track_id": pt["spotify_track_id"]})
                    for pt in self.playlist_tracks if pt["playlist_id"] == pid]

        # New JOIN query: SELECT t.id, ... FROM tracks t JOIN playlist_tracks pt ...
        if "from tracks" in q and "ai_facts is null" in q:
            results = []
            for pt in self.playlist_tracks:
                track_rec = next((t for t in self.tracks if t["id"] == pt["track_id"]), None)
                if track_rec is None:
                    continue
                if track_rec["ai_facts"] is not None:
                    continue
                if not any(p["id"] == pt["playlist_id"] and p["status"] == "upcoming"
                           for p in self.playlists):
                    continue
                results.append(FakeRecord({
                    "id": track_rec["id"],
                    "spotify_track_id": track_rec["spotify_track_id"],
                    "title": track_rec["title"],
                    "artist": track_rec["artist"],
                }))
            return results

        # Legacy: old query pattern (FROM playlist_tracks ... ai_facts is null)
        if "from playlist_tracks" in q and "ai_facts is null" in q:
            results = []
            for pt in self.playlist_tracks:
                track_rec = next((t for t in self.tracks if t["id"] == pt.get("track_id")), None)
                if track_rec and track_rec["ai_facts"] is None:
                    if any(p["id"] == pt["playlist_id"] and p["status"] == "upcoming"
                           for p in self.playlists):
                        results.append(FakeRecord({
                            "id": track_rec["id"],
                            "spotify_track_id": track_rec["spotify_track_id"],
                            "title": track_rec["title"],
                            "artist": track_rec["artist"],
                        }))
            return results

        return []

    def handle_fetchval(self, query: str, args: tuple):
        q = query.strip().lower()

        if "select exists" in q and "users" in q:
            tid = args[0]
            return any(u["telegram_id"] == tid for u in self.users)

        if "select exists" in q and "playlist_tracks" in q:
            if "p.spotify_id" in q or "playlists" in q:
                # JOIN playlists: args are (playlist_spotify_id, track_spotify_id)
                playlist_spotify_id, track_spotify_id = args[0], args[1]
                for p in self.playlists:
                    if p["spotify_id"] == playlist_spotify_id:
                        if any(pt["playlist_id"] == p["id"] and pt["spotify_track_id"] == track_spotify_id
                               for pt in self.playlist_tracks):
                            return True
                return False
            else:
                # Direct: args are (playlist_id, track_spotify_id)
                pid, tid = args[0], args[1]
                return any(t["playlist_id"] == pid and t["spotify_track_id"] == tid
                           for t in self.playlist_tracks)

        # INSERT INTO tracks ... RETURNING id
        if "insert into tracks" in q and "returning id" in q:
            spotify_track_id = args[0]
            title = args[1]
            artist = args[2]
            isrc = args[3] if len(args) > 3 else None
            normalized_title = args[4] if len(args) > 4 else None
            normalized_artist = args[5] if len(args) > 5 else None

            # ON CONFLICT: update if exists
            existing = self._get_track_by_spotify_id(spotify_track_id)
            if existing:
                existing["title"] = title
                existing["artist"] = artist
                return existing["id"]

            track_rec = {
                "id": self._next_track_id,
                "spotify_track_id": spotify_track_id,
                "title": title,
                "artist": artist,
                "isrc": isrc,
                "ai_facts": None,
                "normalized_title": normalized_title,
                "normalized_artist": normalized_artist,
            }
            self._next_track_id += 1
            self.tracks.append(track_rec)
            return track_rec["id"]

        if "is_thematic" in q:
            pid = args[0]
            for p in self.playlists:
                if p["id"] == pid:
                    return p["is_thematic"]
            return None

        if "ai_facts" in q and "spotify_track_id" in q:
            tid = args[0]
            for t in self.tracks:
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

        if "insert into tracks" in q and "insert into playlist_tracks" not in q:
            # Handled by fetchval (RETURNING id), but support execute too
            spotify_track_id = args[0]
            title = args[1]
            artist = args[2]
            existing = self._get_track_by_spotify_id(spotify_track_id)
            if existing:
                existing["title"] = title
                existing["artist"] = artist
                return
            track_rec = {
                "id": self._next_track_id,
                "spotify_track_id": spotify_track_id,
                "title": title,
                "artist": artist,
                "isrc": args[3] if len(args) > 3 else None,
                "ai_facts": None,
                "normalized_title": args[4] if len(args) > 4 else None,
                "normalized_artist": args[5] if len(args) > 5 else None,
            }
            self._next_track_id += 1
            self.tracks.append(track_rec)

        elif "insert into playlist_tracks" in q:
            pid = args[0]
            # New schema: (playlist_id, track_id, spotify_track_id, added_by, added_at)
            if "track_id" in q:
                track_id = args[1]
                spotify_track_id = args[2]
                added_by = args[3] if len(args) > 3 else None
                added_at = args[4] if len(args) > 4 else None
                # ON CONFLICT DO NOTHING
                if any(pt["playlist_id"] == pid and pt["spotify_track_id"] == spotify_track_id
                       for pt in self.playlist_tracks):
                    return
                self.playlist_tracks.append({
                    "id": self._next_pt_id,
                    "playlist_id": pid,
                    "track_id": track_id,
                    "spotify_track_id": spotify_track_id,
                    "added_by_spotify_id": added_by,
                    "added_at": added_at,
                })
                self._next_pt_id += 1
            else:
                # Legacy schema: (playlist_id, spotify_track_id, isrc, title, artist, added_by, added_at)
                tid = args[1]
                if any(pt["playlist_id"] == pid and pt["spotify_track_id"] == tid
                       for pt in self.playlist_tracks):
                    return
                isrc = args[2]
                title = args[3]
                artist = args[4]
                added_by = args[5]
                added_at = args[6]
                # Also create a tracks entry for legacy compat
                track_rec = self._get_track_by_spotify_id(tid)
                if not track_rec:
                    track_rec = {
                        "id": self._next_track_id,
                        "spotify_track_id": tid,
                        "title": title,
                        "artist": artist,
                        "isrc": isrc,
                        "ai_facts": None,
                        "normalized_title": None,
                        "normalized_artist": None,
                    }
                    self._next_track_id += 1
                    self.tracks.append(track_rec)
                self.playlist_tracks.append({
                    "id": self._next_pt_id,
                    "playlist_id": pid,
                    "track_id": track_rec["id"],
                    "spotify_track_id": tid,
                    "added_by_spotify_id": added_by,
                    "added_at": added_at,
                })
                self._next_pt_id += 1

        elif "update tracks set ai_facts" in q:
            facts = args[0]
            track_id = args[1]
            for t in self.tracks:
                if t["id"] == track_id:
                    t["ai_facts"] = facts
                    break

        elif "update playlist_tracks set ai_facts" in q:
            # Legacy support
            facts = args[0]
            pt_id = args[1]
            for pt in self.playlist_tracks:
                if pt["id"] == pt_id:
                    track_rec = next((t for t in self.tracks if t["id"] == pt.get("track_id")), None)
                    if track_rec:
                        track_rec["ai_facts"] = facts
                    break

        elif "delete from playlist_tracks" in q:
            if "any($2)" in q:
                pid = args[0]
                stale_ids = args[1]
                self.playlist_tracks = [
                    pt for pt in self.playlist_tracks
                    if not (pt["playlist_id"] == pid and pt["spotify_track_id"] in stale_ids)
                ]
            else:
                pid, tid = args[0], args[1]
                self.playlist_tracks = [
                    pt for pt in self.playlist_tracks
                    if not (pt["playlist_id"] == pid and pt["spotify_track_id"] == tid)
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
