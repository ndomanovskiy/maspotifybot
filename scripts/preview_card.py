"""Send a preview track card to Telegram for visual testing.

Usage: python scripts/preview_card.py [artist - title]
Default: "USA for Africa - We Are The World - Remastered"
"""
import asyncio
import sys

from aiogram import Bot

BOT_TOKEN = "8679037785:AAFsCHOP3W5adecPEFqLlMHHbegcyguNsbU"
ADMIN_ID = 447516681

# Fake track data for preview
TRACKS = {
    "we are the world": {
        "title": "We Are The World - Remastered",
        "artist": "USA for Africa",
        "album": "We Are the World",
        "track_id": "7rcSwnSk1zpB3VKgxauKAq",
        "cover_url": "https://i.scdn.co/image/ab67616d0000b273e15dcd520adaa89e8b498b41",
        "added_by": "@ndomanovskiy",
    },
    "default_multi": {
        "title": "We Are The World 25 For Haiti",
        "artist": "Artists for Haiti, Michael Jackson, Janet Jackson, Barbra Streisand, Celine Dion",
        "album": "We Are The World 25",
        "track_id": "2PGghMTxe0rGmFbHjekrMR",
        "cover_url": "https://i.scdn.co/image/ab67616d0000b273f3e0e386a0a68fd6c123ae39",
        "added_by": "@testuser",
    },
}


def build_card(track: dict) -> str:
    """Build track card text exactly as the bot does."""
    track_url = f"https://open.spotify.com/track/{track['track_id']}"
    artist_parts = [a.strip() for a in track["artist"].split(",")]
    first_artist = artist_parts[0]
    artist_search_url = f"https://open.spotify.com/search/{first_artist.replace(' ', '%20')}"

    if len(artist_parts) > 3:
        display_artist = ", ".join(artist_parts[:3]) + "…"
    else:
        display_artist = track["artist"]

    text = (
        f"🎵 <a href=\"{track_url}\"><b>{track['title']}</b></a>\n"
        f"🎤 <a href=\"{artist_search_url}\">{display_artist}</a>\n"
        f"💿 {track['album']}\n"
        f"👤 {track['added_by']}\n\n"
        f"💡 Это превью карточки трека для визуального тестирования."
    )
    return text


async def main():
    bot = Bot(token=BOT_TOKEN)

    # Send both variants: single artist and multi-artist
    for key, track in TRACKS.items():
        text = build_card(track)
        if track["cover_url"]:
            await bot.send_photo(ADMIN_ID, photo=track["cover_url"], caption=text, parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_ID, text, parse_mode="HTML")

    await bot.session.close()
    print("Sent preview cards to Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
