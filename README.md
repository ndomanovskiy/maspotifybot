[English](#english) | [Русский](#русский)

---

# English

# MaSpotifyBot (TURDOM Assistant)

Telegram bot for **TURDOM** music sessions — weekly Discord calls where a group of friends listens to Spotify playlists together, voting on tracks in real time.

**Telegram**: [@maspotifybot](https://t.me/maspotifybot)

## Features

- **Live voting** — keep/drop voting with adaptive threshold (ceil 50% of participants)
- **AI track facts** — interesting facts generated via Claude Sonnet (cached + realtime), or written by hand when the AI is unavailable
- **Duplicate detection** — auto-detect by Spotify Track ID and ISRC, auto-remove
- **Genre playlists** — 11 genre sub-playlists, auto-distribute kept tracks after session. The genre comes from Last.fm tags of the track, then of its artists, then Claude; when none of them knows it, the bot asks whoever added the track
- **Session recap** — AI-generated session summary
- **Statistics** — `/stats` (global), `/mystats` (personal), `/history` (session history with pagination)
- **Playlist management** — create, archive, reschedule (collaborative access automatic)
- **Session resilience** — no auto-end on errors, skip guards, race condition protection

## Stack

- **Python 3.12**, aiogram 3, tekore (async Spotify API)
- **PostgreSQL 16** (asyncpg)
- **Claude Sonnet** — track facts, session recap, genres Last.fm does not know; **Last.fm** — genre tags
- **Docker Compose** — bot + db, run locally on the owner's PC

## Commands

| Command | Description |
|---------|-------------|
| `/next` | Link to next playlist |
| `/stats` | Global TURDOM statistics |
| `/mystats` | Personal statistics |
| `/history` | Session history (clickable deeplinks) |
| `/check <url>` | Check track for duplicates |
| `/join` | Join active session |
| `/leave` | Leave session |
| `/session start` | Start session for the upcoming playlist (admin) |
| `/end` | End session (admin) |
| `/kick @username` | Kick participant (admin) |
| `/scan` | Force duplicate check (admin) |
| `/preview <url>` | Preview track card (admin) |
| `/reg <spotify_url>` | Register participant (admin) |

## Setup

```bash
git clone https://github.com/ndomanovskiy/maspotifybot.git
cd maspotifybot
cp .env.example .env
# fill in .env
docker compose up -d
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Tests cover voting logic, session flow, race conditions, caption limits, genre classification, the genre question, the duplicate watcher, HTML escaping, hand-written facts, access control, stats, history, leave/kick.

## Architecture

```
app/
  bot/                   — Telegram layer: user and admin commands, callbacks, session manager, message texts
  services/
    voting.py            — voting and threshold logic
    ai.py                — track facts and recap generation (Claude)
    facts.py             — hand-written track facts when the AI is unavailable
    duplicate_watcher.py — background duplicate check, AI facts, genre question to whoever added a track
    genre_resolver.py    — a track's genre: Last.fm, Claude, or the pick of whoever added it
    lastfm.py            — Last.fm genre tags of tracks and artists
    genre_distributor.py — genre classification and distribution
    playlists.py         — playlist management
  spotify/
    auth.py              — OAuth, token management
    monitor.py           — real-time playback monitoring
tests/                   — pytest
scripts/
  backup.sh              — daily pg_dump → GitHub
```

## Deploy

The bot runs in Docker Compose on the owner's PC: `docker compose up -d --build bot`. There is no CI deploy.

The image installs dependencies with `constraints.txt`, a `pip freeze` of the image that runs now, so a rebuild gets the same versions. To upgrade a package on purpose, change it there and rebuild.

## License

MIT

---

# Русский

# MaSpotifyBot (TURDOM Assistant)

Telegram-бот для музыкальных сессий **TURDOM** — еженедельных Discord-созвонов, где группа друзей совместно слушает Spotify-плейлисты и голосует за треки в реальном времени.

**Telegram**: [@maspotifybot](https://t.me/maspotifybot)

## Возможности

- **Live-голосование** — keep/drop с адаптивным порогом (ceil 50% участников)
- **AI-факты о треках** — генерация через Claude Sonnet (кэш + realtime) или вручную, когда AI недоступен
- **Обнаружение дубликатов** — по Spotify Track ID и ISRC, авто-удаление
- **Жанровые плейлисты** — 11 саб-плейлистов, автораспределение kept-треков после сессии. Жанр берётся из тегов Last.fm у трека, потом у его исполнителей, потом у Claude; если никто не знает, бот спрашивает того, кто добавил трек
- **Рекап сессии** — AI-генерация итогов
- **Статистика** — `/stats` (общая), `/mystats` (персональная), `/history` (история с пагинацией)
- **Управление плейлистами** — создание, архивация, перенос, invite-ссылки
- **Отказоустойчивость** — сессия не падает при ошибках, защита от двойного скипа, race condition guard

## Стек

- **Python 3.12**, aiogram 3, tekore (async Spotify API)
- **PostgreSQL 16** (asyncpg)
- **Claude Sonnet** — факты о треках, рекап сессии, жанры, которых нет в Last.fm; **Last.fm** — жанровые теги
- **Docker Compose** — bot + db, локально на компьютере владельца

## Команды

| Команда | Описание |
|---------|----------|
| `/next` | Ссылка на следующий плейлист |
| `/stats` | Общая статистика TURDOM |
| `/mystats` | Персональная статистика |
| `/history` | История сессий (кликабельные deeplink) |
| `/check <url>` | Проверить трек на дубликат |
| `/join` | Присоединиться к сессии |
| `/leave` | Выйти из сессии |
| `/session start` | Начать сессию по предстоящему плейлисту (админ) |
| `/end` | Завершить сессию (админ) |
| `/kick @username` | Кикнуть участника (админ) |
| `/scan` | Принудительная проверка дубликатов (админ) |
| `/preview <url>` | Превью карточки трека (админ) |
| `/reg <spotify_url>` | Зарегистрировать участника (админ) |

## Установка

```bash
git clone https://github.com/ndomanovskiy/maspotifybot.git
cd maspotifybot
cp .env.example .env
# заполнить .env
docker compose up -d
```

## Тесты

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Тесты покрывают логику голосования, session flow, race conditions, лимиты caption, жанровую классификацию, вопрос о жанре, duplicate watcher, экранирование HTML, ручные факты, access control, статистику, историю, leave/kick.

## Архитектура

```
app/
  bot/                   — Telegram-слой: команды пользователей и админа, callbacks, session manager, тексты сообщений
  services/
    voting.py            — голосование и threshold
    ai.py                — генерация фактов и рекапов (Claude)
    facts.py             — ручные факты, когда AI недоступен
    duplicate_watcher.py — фоновая проверка дубликатов, AI-факты, вопрос о жанре добавившему
    genre_resolver.py    — жанр трека: Last.fm, Claude или выбор того, кто добавил
    lastfm.py            — жанровые теги треков и исполнителей из Last.fm
    genre_distributor.py — классификация и распределение по жанрам
    playlists.py         — управление плейлистами
  spotify/
    auth.py              — OAuth, управление токенами
    monitor.py           — мониторинг playback в реальном времени
tests/                   — pytest
scripts/
  backup.sh              — ежедневный pg_dump → GitHub
```

## Деплой

Бот работает в Docker Compose на компьютере владельца: `docker compose up -d --build bot`. Автодеплоя нет.

Образ ставит зависимости с `constraints.txt` — это `pip freeze` работающего сейчас образа, поэтому пересборка ставит те же версии. Чтобы обновить пакет осознанно, поменяй версию там и пересобери.

## Лицензия

MIT
