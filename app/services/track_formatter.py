"""Unified track display formatting for Telegram HTML messages."""

MAX_ARTISTS = 2


def _track_link(title: str, track_id: str | None) -> str:
    """Create Spotify link for a track."""
    if track_id:
        url = f"https://open.spotify.com/track/{track_id}"
        return f'<a href="{url}"><b>{title}</b></a>'
    return f"<b>{title}</b>"


def format_track(
    title: str,
    artist: str,
    track_id: str | None = None,
    max_artists: int = MAX_ARTISTS,
) -> str:
    """Format track as 'Artist1, Artist2 + — Title' with track hyperlink.

    Artists are plain text. Track title is bold + Spotify link.
    If more than max_artists, appends '+'.

    Returns Telegram HTML string.
    """
    parts = [a.strip() for a in artist.split(",") if a.strip()]

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


def format_album(name: str, max_words: int = 3) -> str:
    """Truncate album name if longer than max_words."""
    words = name.split()
    if len(words) > max_words:
        return " ".join(words[:max_words]) + "..."
    return name
