"""Hand-written track facts, for when the AI provider cannot write them.

Facts are researched in a Claude Code session on request and saved through this module
(it lives in app/ because the Docker image copies only app/):

    docker compose exec -T bot python -m app.services.facts pending <TURDOM number>
    docker compose exec -T bot python -m app.services.facts save < facts.json

`pending` prints the playlist's tracks that have no facts as JSON. `save` reads a JSON object
mapping spotify_track_id to facts in Telegram HTML, refuses facts Telegram could not show
(see facts_html_problems) and writes the rest to tracks.ai_facts, where track cards read them.
"""

import asyncio
import json
import re
import sys
from typing import TypedDict

import asyncpg

MAX_LENGTH = 750  # the same limit the AI prompt sets
ALLOWED_TAGS = {"b", "i", "u", "s", "code"}
_ENTITY = re.compile(r"&(amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")
_ENTITY_WITHOUT_SEMICOLON = re.compile(r"&(amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+)")
_TOKEN = re.compile(r"<[^<>]*>|&[#\w]*;?|[<>]")
_TAG = re.compile(r"<(/?)([a-z]+)>")
_TAG_LIKE = re.compile(r"</?[a-zA-Z]")
_BRACKET_HINT = {"<": "write &lt;", ">": "write &gt;"}


class SaveResult(TypedDict):
    saved: list[str]
    rejected: dict[str, list[str]]
    unknown: list[str]


def facts_html_problems(text: str) -> list[str]:
    """What would stop Telegram from showing these facts; an empty list means they are fine.

    Tags must close on the line they open on: track cards drop whole lines to fit a caption.
    """
    text = text.strip()
    problems = []
    if not text:
        problems.append("empty")
    if len(text) > MAX_LENGTH:
        problems.append(f"longer than {MAX_LENGTH} characters ({len(text)})")

    for number, line in enumerate(text.split("\n"), start=1):
        open_tags: list[str] = []
        for token in (m.group() for m in _TOKEN.finditer(line)):
            if token.startswith("&"):
                if _ENTITY_WITHOUT_SEMICOLON.fullmatch(token):
                    problems.append(f"line {number}: entity {token!r} must end it with ';'")
                elif not _ENTITY.fullmatch(token):
                    problems.append(f"line {number}: bare or unknown entity {token!r}, write &amp;")
                continue
            if token in _BRACKET_HINT:
                problems.append(f"line {number}: bare {token!r}, {_BRACKET_HINT[token]}")
                continue
            if not _TAG_LIKE.match(token):  # "< 10 and 20 >" is text, not a tag
                problems.append(f"line {number}: bare '<' and '>' in {token!r}, write &lt; and &gt;")
                continue
            tag = _TAG.fullmatch(token)
            if not tag or tag.group(2) not in ALLOWED_TAGS:
                problems.append(f"line {number}: not allowed: {token!r}")
                continue
            closing, name = tag.group(1), tag.group(2)
            if not closing:
                if "code" in open_tags:
                    problems.append(f"line {number}: no other tag may go inside <code>: {token!r}")
                open_tags.append(name)
            elif name not in open_tags:
                problems.append(f"line {number}: closes a tag that is not open: {token!r}")
            elif open_tags[-1] != name:
                problems.append(f"line {number}: closes {token!r} before the inner <{open_tags[-1]}>")
                del open_tags[len(open_tags) - 1 - open_tags[::-1].index(name)]
            else:
                open_tags.pop()
        if open_tags:
            problems.append(f"line {number}: not closed on this line: {', '.join(f'<{t}>' for t in open_tags)}")
    return problems


async def pending_tracks(pool: asyncpg.Pool, turdom_number: int) -> list[dict] | None:
    """Tracks of TURDOM#N that have no facts yet, in the order they were added; None if there is no TURDOM#N.

    Numbers are not unique in the schema, so the newest playlist with that number is used.
    """
    async with pool.acquire() as conn:
        playlist_db_id = await conn.fetchval(
            "SELECT id FROM playlists WHERE number = $1 ORDER BY id DESC LIMIT 1", turdom_number,
        )
        if playlist_db_id is None:
            return None
        rows = await conn.fetch(
            """
            SELECT t.spotify_track_id, t.title, t.artist, t.album
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id = $1 AND t.ai_facts IS NULL
            ORDER BY pt.id
            """,
            playlist_db_id,
        )
    return [dict(r) for r in rows]


async def save_facts(pool: asyncpg.Pool, facts: dict[str, object]) -> SaveResult:
    """Write facts that Telegram can show; report the refused ones and unknown track IDs.

    Every entry is checked before the first write, and the writes share one transaction,
    so a database error leaves nothing half-saved. A retry is safe: it overwrites.
    """
    if not isinstance(facts, dict):
        raise TypeError("expected a JSON object mapping spotify_track_id to facts")
    result: SaveResult = {"saved": [], "rejected": {}, "unknown": []}
    good: dict[str, str] = {}
    for spotify_track_id, text in facts.items():
        problems = ["not a string"] if not isinstance(text, str) else facts_html_problems(text)
        if problems:
            result["rejected"][spotify_track_id] = problems
        else:
            good[spotify_track_id] = text.strip()

    async with pool.acquire() as conn:
        async with conn.transaction():
            for spotify_track_id, text in good.items():
                track_db_id = await conn.fetchval(
                    "SELECT id FROM tracks WHERE spotify_track_id = $1", spotify_track_id,
                )
                if track_db_id is None:
                    result["unknown"].append(spotify_track_id)
                    continue
                await conn.execute("UPDATE tracks SET ai_facts = $1 WHERE id = $2", text, track_db_id)
                result["saved"].append(spotify_track_id)
    return result


_USAGE = "usage: python -m app.services.facts pending <TURDOM number> | save < facts.json"


EXIT_OK, EXIT_BAD_INPUT, EXIT_SOME_NOT_SAVED = 0, 2, 3  # a crash exits with 1 and a traceback


async def _main(args: list[str]) -> int:
    from app.config import settings

    facts: dict = {}
    if args[:1] == ["pending"] and len(args) == 2 and args[1].isdecimal():
        command = "pending"
    elif args == ["save"]:
        command = "save"
        try:
            facts = json.load(sys.stdin)
        except ValueError as e:  # bad JSON, or a file that is not UTF-8
            print(f"not valid UTF-8 JSON: {e}", file=sys.stderr)
            return EXIT_BAD_INPUT
        if not isinstance(facts, dict):
            print("expected a JSON object mapping spotify_track_id to facts", file=sys.stderr)
            return EXIT_BAD_INPUT
    else:
        print(_USAGE, file=sys.stderr)
        return EXIT_BAD_INPUT

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=1)
    try:
        if command == "pending":
            tracks = await pending_tracks(pool, int(args[1]))
            if tracks is None:
                print(f"no playlist TURDOM#{args[1]}", file=sys.stderr)
                return EXIT_BAD_INPUT
            print(json.dumps(tracks, ensure_ascii=False, indent=2))
            return EXIT_OK
        result = await save_facts(pool, facts)
    finally:
        await pool.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_SOME_NOT_SAVED if result["rejected"] or result["unknown"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
