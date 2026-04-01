# MaSpotifyBot (TURDOM Assistant)

Telegram-бот для музыкальных сессий **TURDOM** — еженедельных Discord-созвонов, где группа друзей совместно слушает музыку из Spotify.

## Что делает

- **Live voting** — голосование keep/drop по трекам во время сессии с адаптивным порогом (ceil 50% от участников)
- **AI track facts** — генерация интересных фактов о треках через GPT-4o-mini (фоновая + realtime)
- **Duplicate detection** — автоматическое обнаружение дубликатов по Spotify Track ID и ISRC, авто-удаление
- **Genre playlists** — 11 жанровых саб-плейлистов, автоматическое распределение kept-треков после сессии
- **Session recap** — AI-генерация итогов сессии
- **Statistics** — `/stats` (общая), `/mystats` (персональная), `/history` (история сессий с пагинацией)
- **Playlist management** — создание, архивация, reschedule, invite-ссылки
- **Session recovery** — восстановление активной сессии при рестарте бота

## Stack

- **Python 3.12**, aiogram 3, tekore (async Spotify API)
- **PostgreSQL 16** (asyncpg)
- **OpenAI GPT-4o-mini** — track facts, session recap
- **Docker Compose** — bot + db
- **GitHub Actions** — CI/CD (pytest → SSH deploy)

## Команды бота

| Команда | Описание |
|---------|----------|
| `/next` | Ссылка на следующий плейлист |
| `/stats` | Общая статистика TURDOM |
| `/mystats` | Персональная статистика |
| `/history` | История сессий |
| `/check <url>` | Проверить трек на дубликат |
| `/join` | Присоединиться к сессии |
| `/leave` | Выйти из сессии |
| `/session <url>` | Начать сессию (admin) |
| `/end` | Завершить сессию (admin) |
| `/create` | Создать следующий плейлист (admin) |
| `/setnextlink <url>` | Установить invite-ссылку (admin) |
| `/kick @username` | Кикнуть участника из сессии (admin) |
| `/scan` | Принудительная проверка дубликатов (admin) |
| `/reg <spotify_url>` | Зарегистрировать участника (admin) |

## Запуск

```bash
cp .env.example .env
# заполнить .env
docker compose up -d
```

## Тесты

```bash
pip install .[dev]
pytest tests/ -v
```

117 тестов: voting threshold, genre classification, duplicate watcher, access control, stats, history, leave/kick.

## Структура

```
app/
  bot/handlers.py       — Telegram команды и callbacks
  services/
    voting.py           — голосование и threshold
    ai.py               — генерация фактов и рекапов
    duplicate_watcher.py — фоновая проверка дубликатов + генерация AI facts
    genre_distributor.py — классификация и распределение по жанрам
    playlists.py         — управление плейлистами
  spotify/
    auth.py             — OAuth, token management
    monitor.py          — real-time мониторинг playback
  db/
    schema.py           — схема БД + миграции
    pool.py             — asyncpg connection pool
tests/                  — pytest
scripts/
  backup.sh             — daily pg_dump → GitHub
```
