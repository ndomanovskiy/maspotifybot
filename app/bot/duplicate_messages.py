"""Texts of the duplicate-check messages.

Pure functions, no I/O. Track titles, artists, playlist names and user names come from Spotify
and users, so every one of them is escaped here before it goes into Telegram HTML.
"""

from app.services.track_formatter import format_track
from app.utils import escape_html

# How a duplicate notification names each kind of match from check_duplicate
MATCH_LABELS = {
    "fuzzy_exact": "🔍 нормализованное совпадение",
    "fuzzy_contains": "🔍 слова совпадают",
    "fuzzy_levenshtein": "🔍 похожее название",
    "exact": "🎯 точное",
    "isrc": "🔗 ISRC",
}

_SIBLING_KINDS = {"self": "это версия", "other": "уже есть версия", "both": "обе — версии"}


def _playlist_link(name: str | None, url: str | None) -> str:
    return f'<a href="{escape_html(url)}">{escape_html(name)}</a>' if url else escape_html(name)


def _added_line(added_by_name: str | None) -> str:
    return f"\n👤 Добавил: {escape_html(added_by_name)}" if added_by_name else ""


def auto_removed_text(
    track_title: str | None, artist: str | None, track_id: str | None, duplicates: list[dict],
    playlist_name: str | None, playlist_url: str | None, added_by_name: str | None = None,
) -> str:
    """A hard duplicate was removed from the playlist automatically."""
    dup_lines = [
        f"  {MATCH_LABELS.get(d['match'], '🔍 похож')} — {_playlist_link(d['playlist'], d.get('url'))}"
        for d in duplicates
    ]
    return "\n".join([
        "🗑 <b>Дубликат удалён!</b>",
        "",
        f"🎵 {format_track(track_title, artist, track_id)}{_added_line(added_by_name)}",
        f"Удалён из: {_playlist_link(playlist_name, playlist_url)}",
        "",
        "Уже был:",
        *dup_lines,
    ])


def possible_duplicate_text(
    track_title: str | None, artist: str | None, track_id: str | None, duplicates: list[dict],
    playlist_name: str | None, added_by_name: str | None = None,
) -> str:
    """Ask whether a lookalike, or a track dropped before, should stay."""
    match_lines = []
    for d in duplicates:
        label = "❌ дропнут ранее" if d.get("session_status") == "drop" else MATCH_LABELS.get(d["match"], "🔍 похож")
        match_lines.append(f"  {label} — {_playlist_link(d['playlist'], d.get('url'))}")
        match_lines.append(f"  {escape_html(d['title'])} — {escape_html(d['artist'])}")
    if duplicates and all(d.get("session_status") == "drop" for d in duplicates):
        header, footer = "🔄 <b>Второй шанс?</b>", "Этот трек уже дропали. Оставить?"
    else:
        header, footer = "🔍 <b>Возможный дубликат</b>", "Удалить трек из плейлиста?"
    return "\n".join([
        header,
        "",
        f"🎵 {format_track(track_title, artist, track_id)}{_added_line(added_by_name)}",
        f"в <b>{escape_html(playlist_name)}</b>",
        "",
        "Похож на:",
        *match_lines,
        "",
        footer,
    ])


def dropped_before_text(
    track_title: str | None, artist: str | None, track_id: str | None, drops: list[dict],
    playlist_name: str | None,
) -> str:
    """Warn that the track was dropped in an earlier session."""
    drop_lines = [
        f"  ❌ {escape_html(d['playlist'])} ({escape_html(d['date'])}) — добавил {escape_html(d['added_by'])}"
        for d in drops
    ]
    return "\n".join([
        "⚠️ <b>Ранее дропнутый трек!</b>",
        "",
        f"🎵 {format_track(track_title, artist, track_id)}",
        f"Добавлен в <b>{escape_html(playlist_name)}</b>",
        "",
        "Был дропнут:",
        *drop_lines,
        "",
        "Оставить или убрать?",
    ])


def sibling_text(
    track_title: str | None, artist: str | None, track_id: str | None, siblings: list[dict],
    playlist_name: str | None,
) -> str:
    """Note that a modified version (remix, sped up) of a known track was added. Informational only."""
    sibling_lines = [
        f"  🔁 {_SIBLING_KINDS.get(s['kind'], 'версия')}: {escape_html(s['title'])} — {escape_html(s['artist'])} "
        f"<i>в {escape_html(s['playlist'])}</i>"
        for s in siblings[:5]
    ]
    if len(siblings) > 5:
        sibling_lines.append(f"  <i>…и ещё {len(siblings) - 5}</i>")
    return "\n".join([
        "ℹ️ <b>Изменённая версия — не дубль</b>",
        "",
        f"🎵 {format_track(track_title, artist, track_id)}",
        f"Добавлен в <b>{escape_html(playlist_name)}</b>",
        "",
        *sibling_lines,
    ])
