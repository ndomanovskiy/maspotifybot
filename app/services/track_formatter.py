"""Unified track display formatting for Telegram HTML messages."""

from app.utils import escape_html

MAX_ARTISTS = 2


def _track_link(title: str | None, track_id: str | None) -> str:
    """Create Spotify link for a track."""
    title = escape_html(title)
    if track_id:
        url = f"https://open.spotify.com/track/{escape_html(track_id)}"
        return f'<a href="{url}"><b>{title}</b></a>'
    return f"<b>{title}</b>"


def format_track(
    title: str | None,
    artist: str | None,
    track_id: str | None = None,
    max_artists: int = MAX_ARTISTS,
) -> str:
    """Format track as 'Artist1, Artist2 + — Title' with track hyperlink.

    Artists are plain text. Track title is bold + Spotify link.
    If more than max_artists, appends '+'.

    Returns Telegram HTML string; title and artists are escaped here, so pass them raw.
    """
    parts = [escape_html(a.strip()) for a in (artist or "?").split(",") if a.strip()]

    if len(parts) > max_artists:
        artists_str = ", ".join(parts[:max_artists]) + " +"
    else:
        artists_str = ", ".join(parts)

    track_str = _track_link(title, track_id)

    return f"{artists_str} — {track_str}"


def format_track_plain(
    title: str,
    artist: str,
    max_artists: int = MAX_ARTISTS,
) -> str:
    """Format track without hyperlinks (for AI context, logs, etc.)."""
    parts = [a.strip() for a in artist.split(",") if a.strip()]

    if len(parts) > max_artists:
        artists_str = ", ".join(parts[:max_artists]) + " +"
    else:
        artists_str = ", ".join(parts)

    return f"{artists_str} — {title}"


def format_album(name: str | None, max_words: int = 3) -> str:
    """Album name for Telegram HTML: truncated if longer than max_words, then escaped; pass it raw."""
    words = (name or "").split()
    if len(words) > max_words:
        return escape_html(" ".join(words[:max_words]) + "...")
    return escape_html(name or "")


def build_track_caption(
    title: str,
    artist: str,
    album: str,
    track_id: str | None,
    facts: str = "",
    added_by_text: str = "",
    max_caption: int = 1024,
) -> str:
    """Build a full track card caption with optional facts, trimmed to max_caption.

    Shared by /preview (admin command) and live session track cards.
    Title, artist and album are escaped here; facts and added_by_text are taken as ready HTML,
    so escape the name inside added_by_text yourself. added_by_text should already include
    the leading '\\n' if provided (e.g. '\\n👤 @foo').
    """
    track_display = format_track(title, artist, track_id)
    album_display = format_album(album)
    header = (
        f"🎵 {track_display}\n"
        f"💿 {album_display}"
        f"{added_by_text}"
    )
    facts_text = f"\n\n💡 {facts}" if facts else ""
    text = f"{header}{facts_text}"
    if len(text) <= max_caption or not facts_text:
        return text

    # Trim facts line-by-line to fit
    available = max_caption - len(header) - 3  # '\n\n💡 '
    if available <= 30:
        return header
    trimmed: list[str] = []
    total = 0
    for line in facts.split("\n"):
        if total + len(line) + 1 <= available:
            trimmed.append(line)
            total += len(line) + 1
        else:
            break
    if not trimmed:
        return header
    return f"{header}\n\n💡 " + "\n".join(trimmed)
