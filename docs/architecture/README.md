# MaSpotifyBot — Architecture Documentation

## Overview

Collaborative music listening bot: Telegram + Spotify. Users add tracks to shared playlist, vote keep/drop during live session, kept tracks distribute to genre playlists.

## Documents

| Document | Description |
|----------|-------------|
| [01-modules.md](01-modules.md) | All services, modules, dependencies |
| [02-database.md](02-database.md) | Database schema and relationships |
| [03-flows.md](03-flows.md) | Main user flows with sequence diagrams |
| [04-commands.md](04-commands.md) | All bot commands reference |

## Tech Stack

- **Bot:** Python 3.12, aiogram v3
- **Spotify:** tekore (async)
- **DB:** PostgreSQL, asyncpg
- **AI:** OpenAI GPT-4o-mini
- **Deploy:** Docker, GitHub Actions CI/CD, Selectel VPS
