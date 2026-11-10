# maspotify

Collaborative music listening Telegram bot for TURDOM community. Users add tracks to shared Spotify playlist, vote keep/drop during live sessions, kept tracks auto-distribute to genre playlists.

## Stack

- **Python 3.12**, aiogram v3 (Telegram), tekore (Spotify async), asyncpg (PostgreSQL)
- **AI:** Claude Sonnet for track facts, session recap commentary and easter egg analysis, with no fallback: when the Anthropic call fails, those AI-written parts come out empty (the recap still sends its stats, the teaser has a fixed text). GPT-4o-mini for genres.
- **Deploy:** local Docker on the owner's PC since 2026-09-30 (`docker compose up -d --build bot`). Spotify traffic exits through the host's Clash Verge.

## Project Structure

```
app/
├── main.py                 — Entry point
├── config.py               — Pydantic settings from env
├── utils.py                — display_name, escape_html
├── bot/                    — Telegram bot layer
│   ├── __init__.py         — Router wiring + setup_bot()
│   ├── core.py             — Shared: bot, dp, pool, helpers, decorators
│   ├── session_manager.py  — SessionManager singleton (all session state)
│   ├── callbacks.py        — Callback query handlers
│   ├── playlist_notify.py  — Admin picker of who to notify about a new playlist
│   ├── duplicate_messages.py — Texts of the duplicate-check messages (escaped HTML)
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
The `core.py` helpers (`send`, `reply`, `edit_text`, ...) and any call with `parse_mode="HTML"` send HTML: escape anything from Spotify or users in them (track titles, artists, albums, playlist names, user names, secrets) with `escape_html()` from `app.utils` (it turns None into `?`). A bare `message.answer(...)` without `parse_mode` sends plain text: do not escape there, or people see `&lt;`.
- `format_track`, `format_album` and the texts in `app/bot/duplicate_messages.py` escape for you: pass raw text, or it gets escaped twice.
- `build_track_caption` escapes title, artist and album itself, but takes `facts` and `added_by_text` as ready HTML: escape the name inside `added_by_text` yourself.

### Error handling
Never use bare `except Exception: pass`. Always log: `except Exception as e: log.debug(f"...")` or `log.warning(...)`.

## Key Files

| File | Lines | What |
|------|-------|------|
| `app/bot/core.py` | 151 | Shared infrastructure, helpers, decorators |
| `app/bot/session_manager.py` | 695 | SessionManager — all session lifecycle |
| `app/bot/commands/user.py` | 799 | User-facing commands |
| `app/bot/commands/admin.py` | 757 | Admin commands |
| `app/bot/callbacks.py` | 643 | All callback handlers |
| `app/bot/playlist_notify.py` | 193 | New playlist notification picker |
| `app/bot/duplicate_messages.py` | 118 | Duplicate-check message texts |
| `app/services/facts.py` | 184 | Hand-written track facts: check and save |
| `app/services/admin_commands.py` | 737 | Distribute, recap, close, create_next |
| `app/db/schema.py` | 384 | Schema + 70 versioned migrations |

## Running

```bash
# Tests
python -m pytest tests/ -q

# Run bot and database
docker compose up -d --build bot
```

The Spotify refresh token lives 6 months and cannot be extended. On `invalid_grant`, run `/auth` in the bot.

### Hand-written track facts

When the bot cannot write track facts (for example, the Anthropic credit balance is empty), write them by hand when asked. Run these from Bash in the repo root (PowerShell 5.1 mangles Cyrillic in the pipe):

```bash
docker compose exec -T bot python -m app.services.facts pending <TURDOM number>   # tracks without facts, as JSON
docker compose exec -T bot python -m app.services.facts save < facts.json         # {spotify_track_id: facts}
```

Write each like the AI prompt in `app/services/ai.py` asks: five short facts in Russian, one per line, each starting with an emoji; research them, do not guess. `save` itself only checks what Telegram needs: tags `<b>`, `<i>`, `<u>`, `<s>`, `<code>` closed on the same line, no other tags inside `<code>`, `&`, `<` and `>` in text written as `&amp;`, `&lt;`, `&gt;`, no more than 750 characters. It refuses the whole entry otherwise and saves the rest. Exit codes: 0 all saved, 2 bad input (nothing saved), 3 some entries refused or their track ID unknown (see the printed JSON), 1 a crash. The admin «regen facts» button keeps the old facts when generation fails.

## Architecture docs
See `docs/architecture/` for detailed diagrams and flows.
