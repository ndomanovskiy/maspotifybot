"""Shared utility functions with no layer-specific dependencies."""


def display_name(username: str | None, name: str | None) -> str:
    """Build user display name: @username if set, else telegram_name, else '?'.

    Empty string is treated as unset (same as None).
    """
    if username:
        return f"@{username}"
    return name or "?"
