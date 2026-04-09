# maspotify

Collaborative music listening Telegram bot for TURDOM community. Users add tracks to shared Spotify playlist, vote keep/drop during live sessions, kept tracks auto-distribute to genre playlists.

## Stack

- **Python 3.10+**, aiogram v3 (Telegram), tekore (Spotify async), asyncpg (PostgreSQL)
- **AI:** OpenAI GPT-4o-mini (track facts, session recap, easter egg analysis)
- **Deploy:** Docker, GitHub Actions CI/CD, Selectel VPS

## Project Structure

```
app/
├── main.py                 — Entry point
├── config.py               — Pydantic settings from env
├── bot/                    — Telegram bot layer
│   ├── __init__.py         — Router wiring + setup_bot()
│   ├── core.py             — Shared: bot, dp, pool, helpers, decorators
│   ├── session_manager.py  — SessionManager singleton (all session state)
│   ├── callbacks.py        — Callback query handlers
│   └── commands/
│       ├── user.py         — User commands (/start, /join, /stats, etc.)
│       └── admin.py        — Admin commands (/session, /distribute, etc.)
├── services/               — Business logic (no Telegram dependency)
├── spotify/                — Spotify API (auth, playback monitor)
└── db/                     — asyncpg pool + versioned schema migrations
```

## Conventions

### Message sending
Always use helpers from `core.py`, never raw `bot.send_message`:
- `send(chat_id, text)` — HTML, no preview by default
- `reply(message, text)` — same for message.answer
- `send_photo(chat_id, photo, caption)` — photo with HTML caption
- `edit_text(chat_id, msg_id, text)` — edit with HTML
- Pass `preview=True` to enable link preview

### Access control
Use decorators, not inline `if not is_admin`:
- `@require_admin` — admin-only commands
- `@require_registered` — registered users only
- `@require_admin_callback` — admin-only callbacks

### Spotify ID extraction
One function for all entities: `extract_spotify_id(url_or_id, entity="track"|"playlist"|"user")`

### Session state
All session state lives in `SessionManager` singleton (`session` in session_manager.py). Never use module-level globals for session data.

### Database migrations
Append to `VERSIONED_MIGRATIONS` in `schema.py`. Never reorder or edit existing entries. Each migration has a version number, description, and SQL.

### HTML safety
Escape user input with `html.escape()` before embedding in HTML messages.

### Error handling
Never use bare `except Exception: pass`. Always log: `except Exception as e: log.debug(f"...")` or `log.warning(...)`.

## Key Files

| File | Lines | What |
|------|-------|------|
| `app/bot/core.py` | 147 | Shared infrastructure, helpers, decorators |
| `app/bot/session_manager.py` | 535 | SessionManager — all session lifecycle |
| `app/bot/commands/user.py` | 700 | User-facing commands |
| `app/bot/commands/admin.py` | 575 | Admin commands |
| `app/bot/callbacks.py` | 420 | All callback handlers |
| `app/services/admin_commands.py` | 666 | Distribute, recap, close, create_next |
| `app/db/schema.py` | 250 | Schema + 32 versioned migrations |

## Running

```bash
# Tests
python -m pytest tests/ -q

# Local
docker-compose up -d
python -m app.main
```

## Architecture docs
See `docs/architecture/` for detailed diagrams and flows.
