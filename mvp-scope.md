# MaSpotifyBot — MVP Scope Definition

> Version: 1.0
> Date: 2026-03-30
> Author: Obi-Wan (Planning)
> Status: Proposal — pending CEO approval

---

## MVP-0: "Минимальная среда" (первая живая сессия)

**Цель:** провести одну полную сессию TURDOM с ботом вместо голосового голосования.
**Критерий успеха:** бот используется всю сессию без fallback к ручному процессу.

### Включено в MVP-0

| # | User Story | Модуль | Обоснование |
|---|-----------|--------|-------------|
| 1 | Start session linked to playlist | Session Manager | Без этого нет сессии |
| 2 | Notification when session starts | Telegram Bot | Участники должны знать что началось |
| 3 | See current track (cover, title, artist) | Mini App | Базовый UI для сессии |
| 7 | Detect pause/resume/skip | Spotify Monitor | Синхронизация состояния |
| 8 | End session | Session Manager | Завершение сессии |
| 9 | Vote keep/drop | Vote Engine | Core value proposition |
| 10 | Auto-remove on threshold | Vote Engine + Spotify | Core value proposition |
| 11 | Skip after drop | Vote Engine + Spotify | Логическое следствие drop |
| 12 | Notification on drop decision | Mini App | Обратная связь участникам |
| 13 | Adaptive voting threshold | Vote Engine | 3-5 участников, нужна гибкость |
| 35 | Register participants (Telegram ↔ Spotify) | User Registry | Без маппинга не знаем кто добавил трек |
| 37 | Manage participant list | User Registry | Админка для Nikita |

**Итого: 12 user stories (из 47)**

### Явно НЕ включено в MVP-0

- AI-факты о треках (stories 19-21)
- AI-диалог в чате (stories 22-26)
- Рейтинги по 5 критериям (stories 15-18)
- Дубликат-детекция (stories 27-31)
- Session recap (stories 32-34)
- Статистика и лидерборды (stories 44-47)
- Плейлист-менеджмент: архивация, импорт (stories 38-40)
- Настройки: тема, язык (stories 41-42)
- Админ-настройки в Mini App (story 43)
- Аватар добавившего трек (story 4)
- Таймер плейбека (story 5)
- Название сессии в UI (story 6)
- Конфигурируемый threshold (story 14) — хардкодим 2/4
- Онбординг нового участника (story 36)

### Технический scope MVP-0

| Компонент | Что делаем | Что НЕ делаем |
|-----------|-----------|---------------|
| Backend | FastAPI + aiogram 3 + tekore | AI layer, cron jobs |
| Database | PostgreSQL: users, sessions, votes | tracks DB, ratings, duplicates |
| Frontend | 1 экран: текущий трек + кнопки vote | History, Stats, Settings |
| Real-time | WebSocket: track changes + vote results | Lyrics, AI facts |
| Infra | Docker Compose, single VM | CI/CD, monitoring, backups |
| Auth | Hardcoded Nikita's Spotify token в .env | Token refresh, multi-user auth |

### Риски MVP-0

1. **Token expiry mid-session** — митигация: ручной refresh перед сессией, документировать процесс
2. **Race condition на голосах** — митигация: serialized vote processing (queue)
3. **Spotify rate limit** — митигация: polling 5 sec (не 3), backoff on 429

### Definition of Done

- [ ] Nikita может начать сессию командой `/start_session <playlist_url>`
- [ ] Все участники получают уведомление с кнопкой открыть Mini App
- [ ] Mini App показывает текущий трек (обложка + название + артист)
- [ ] Каждый может нажать Keep или Drop
- [ ] При 2 drop-голосах трек удаляется из плейлиста и скипается
- [ ] Все видят уведомление о решении
- [ ] Nikita может завершить сессию командой `/end_session`
- [ ] Сессия работает стабильно 2 часа (~40 треков)

---

## MVP-1: "Полная среда" (+2-3 недели после MVP-0)

**Добавляем:** duplicate detection, session recap, playback timer, session name, avatar, threshold config.

| # | User Stories | Обоснование |
|---|-------------|-------------|
| 4-6 | Avatar, timer, session name | UI polish |
| 14 | Configurable threshold | Гибкость |
| 27-31 | Duplicate detection | Вторая по важности боль |
| 32-34 | Session recap | Документирование сессий |
| 36 | Onboarding flow | Удобство для новых |
| 38-39 | Playlist archival + registration | Менеджмент плейлистов |

---

## MVP-2: "Умная среда" (+4-6 недель после MVP-1)

**Добавляем:** AI-факты, AI-диалог, рейтинги, статистика.

| # | User Stories | Обоснование |
|---|-------------|-------------|
| 15-18 | Rating engine (5 criteria) | Глубокая аналитика |
| 19-21 | AI track facts + lyrics | Обогащение обсуждения |
| 22-26 | AI chat dialog | Conversational interface |
| 40 | Import 2649 tracks | Историческая база |
| 41-43 | Settings + admin | Кастомизация |
| 44-47 | Stats, leaderboards, history | Геймификация |

---

## Решения, требующие подтверждения CEO

1. **MVP-0 = 12 stories** — достаточно ли для первой сессии?
2. **Threshold хардкод 2/4** — ок для начала?
3. **Без AI в MVP-0** — бот полезен без фактов?
4. **Без дубликатов в MVP-0** — продолжаем ручной Tourdom Check?
5. **Token в .env** — ок для MVP, планируем refresh flow в MVP-1?
