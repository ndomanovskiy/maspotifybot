[English](#english) | [Русский](#русский)

---

# English

# MaSpotifyBot (TURDOM Assistant)

Telegram bot for **TURDOM** music sessions — weekly Discord calls where a group of friends listens to Spotify playlists together, voting on tracks in real time.

**Telegram**: [@maspotifybot](https://t.me/maspotifybot)

## Features

- **Live voting** — keep/drop voting with adaptive threshold (ceil 50% of participants)
- **AI track facts** — interesting facts generated via GPT-4o-mini (cached + realtime)
- **Duplicate detection** — auto-detect by Spotify Track ID and ISRC, auto-remove
- **Genre playlists** — 11 genre sub-playlists, auto-distribute kept tracks after session
- **Session recap** — AI-generated session summary
- **Statistics** — `/stats` (global), `/mystats` (personal), `/history` (session history with pagination)
- **Playlist management** — create, archive, reschedule (collaborative access automatic)
- **Session resilience** — no auto-end on errors, skip guards, race condition protection

## Stack

- **Python 3.12**, aiogram 3, tekore (async Spotify API)
- **PostgreSQL 16** (asyncpg)
- **OpenAI GPT-4o-mini** — track facts, session recap
- **Docker Compose** — bot + db
- **GitHub Actions** — CI/CD (pytest → SSH deploy)

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
| `/session <url>` | Start session (admin) |
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

179 tests: voting logic, session flow, race conditions, caption limits, genre classification, duplicate watcher, access control, stats, history, leave/kick.

## Architecture

```
app/
  bot/handlers.py        — Telegram commands and callbacks
  services/
    voting.py            — voting and threshold logic
    ai.py                — track facts and recap generation
    duplicate_watcher.py — background duplicate check + AI facts
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

Pushes to `main` trigger GitHub Actions → pytest → SSH → `docker compose up -d --build`.

## License

MIT

---

# Русский

# MaSpotifyBot (TURDOM Assistant)

Telegram-бот для музыкальных сессий **TURDOM** — еженедельных Discord-созвонов, где группа друзей совместно слушает Spotify-плейлисты и голосует за треки в реальном времени.

**Telegram**: [@maspotifybot](https://t.me/maspotifybot)

## Возможности

- **Live-голосование** — keep/drop с адаптивным порогом (ceil 50% участников)
- **AI-факты о треках** — генерация через GPT-4o-mini (кэш + realtime)
- **Обнаружение дубликатов** — по Spotify Track ID и ISRC, авто-удаление
- **Жанровые плейлисты** — 11 саб-плейлистов, автораспределение kept-треков после сессии
- **Рекап сессии** — AI-генерация итогов
- **Статистика** — `/stats` (общая), `/mystats` (персональная), `/history` (история с пагинацией)
- **Управление плейлистами** — создание, архивация, перенос, invite-ссылки
- **Отказоустойчивость** — сессия не падает при ошибках, защита от двойного скипа, race condition guard

## Стек

- **Python 3.12**, aiogram 3, tekore (async Spotify API)
- **PostgreSQL 16** (asyncpg)
- **OpenAI GPT-4o-mini** — факты о треках, рекап сессии
- **Docker Compose** — bot + db
- **GitHub Actions** — CI/CD (pytest → SSH деплой)

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
| `/session <url>` | Начать сессию (админ) |
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

179 тестов: логика голосования, session flow, race conditions, лимиты caption, жанровая классификация, duplicate watcher, access control, статистика, история, leave/kick.

## Архитектура

```
app/
  bot/handlers.py        — Telegram-команды и callbacks
  services/
    voting.py            — голосование и threshold
    ai.py                — генерация фактов и рекапов
    duplicate_watcher.py — фоновая проверка дубликатов + AI-факты
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

Push в `main` → GitHub Actions → pytest → SSH → `docker compose up -d --build`.

## Лицензия

MIT
