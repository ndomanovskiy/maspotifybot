"""Shared utility functions with no layer-specific dependencies."""

import html


def escape_html(value: object) -> str:
    """Text for a Telegram HTML message: escaped, with '?' for a missing value."""
    return "?" if value is None else html.escape(str(value))


def display_name(username: str | None, name: str | None) -> str:
    """Build user display name: @username if set, else telegram_name, else '?'.

    Empty string is treated as unset (same as None).
    """
    if username:
        return f"@{username}"
    return name or "?"
