"""Unified track display formatting for Telegram HTML messages."""

MAX_ARTISTS = 3


def _artist_link(name: str) -> str:
    """Create Spotify search link for an artist."""
    search_url = f"https://open.spotify.com/search/{name.strip().replace(' ', '%20')}"
    return f'<a href="{search_url}">{name.strip()}</a>'


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
    """Format track as '[Artist1, Artist2, Artist3...] — Title' with hyperlinks.

    Artists link to Spotify search. Track title links to Spotify track page.
    If more than max_artists, truncates with '...'.

    Returns Telegram HTML string.
    """
    parts = [a.strip() for a in artist.split(",") if a.strip()]

    # Only link artists if we have meaningful names
    has_valid_artists = parts and all(len(a) > 1 for a in parts)

    if has_valid_artists:
        display = parts[:max_artists]
        linked = ", ".join(_artist_link(a) for a in display)
        artists_str = f"{linked}..." if len(parts) > max_artists else linked
    else:
        artists_str = artist

    track_str = _track_link(title, track_id)

    return f"{artists_str} — {track_str}"


def format_track_plain(
    title: str,
    artist: str,
    max_artists: int = MAX_ARTISTS,
) -> str:
    """Format track without hyperlinks (for AI context, logs, etc.)."""
    parts = [a.strip() for a in artist.split(",")]

    if len(parts) > max_artists:
        artists_str = ", ".join(parts[:max_artists]) + "..."
    else:
        artists_str = artist

    return f"{artists_str} — {title}"
