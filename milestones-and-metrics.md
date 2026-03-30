# MaSpotifyBot — Milestones & Success Metrics

> Version: 1.0
> Date: 2026-03-30
> Author: Obi-Wan (Planning)

---

## Milestones

### M0: PRD Finalized (текущий этап)
- Все департаменты ревьюят PRD ✅
- MVP scope утверждён CEO
- PRD v1.1 опубликован
- **Выход:** PRD v1.1 + утверждённый MVP-0 scope

### M1: Foundation (MVP-0 Development)
- Backend skeleton: FastAPI + aiogram + tekore
- Database schema: users, sessions, votes
- Spotify Monitor: polling + track change detection
- Vote Engine: keep/drop + auto-remove
- Mini App: 1 экран (текущий трек + vote buttons)
- WebSocket: real-time track updates + vote results
- Docker Compose: backend + DB + frontend
- User Registry: Telegram ↔ Spotify mapping
- **Выход:** деплой на Selectel VM, готов к первой сессии

### M2: First Live Session (MVP-0 Validation)
- Pre-session checklist выполнен (за 1 час до сессии)
- Сессия TURDOM проведена с ботом от начала до конца
- Post-session retro: что сломалось, что улучшить
- **Выход:** bug list + improvement list из первой сессии

### M3: Stabilization (MVP-0 → MVP-1 bridge)
- Фиксы багов из первой сессии
- 2-3 успешных сессии подряд без fallback
- **Выход:** стабильный MVP-0

### M4: MVP-1 Release
- Duplicate detection (exact + ISRC)
- Session recap generation
- UI polish (avatar, timer, session name)
- Configurable threshold
- Playlist archival
- **Выход:** полноценный бот для еженедельных сессий

### M5: MVP-2 Release
- AI track facts + lyrics
- AI chat dialog
- Rating engine (5 criteria)
- Stats + leaderboards
- Full track database (2649 tracks import)
- Settings (theme, language, admin)
- **Выход:** feature-complete бот

---

## Success Metrics

### MVP-0 Success (M2-M3)

| Metric | Target | Как измеряем |
|--------|--------|-------------|
| Session completion rate | 100% (без fallback к ручному) | Бот работает всю сессию 2h |
| Voting participation | ≥3 из 4 участников голосуют | DB: votes per session per user |
| Vote latency | <5 sec от нажатия до удаления трека | Логи: vote timestamp → Spotify API call |
| Uptime during session | 100% | Нет перезагрузок/падений за 2h |
| Track removal accuracy | 100% правильных удалений | Нет случайных удалений не тех треков |

### MVP-1 Success (M4)

| Metric | Target | Как измеряем |
|--------|--------|-------------|
| Consecutive sessions without issues | ≥3 | Retro log |
| Duplicate detection rate | ≥90% exact matches caught | Manual audit vs Tourdom Check |
| Recap satisfaction | Участники читают и обсуждают | Качественная обратная связь |
| Zero manual playlist management | Архивация полностью через бот | Nikita не заходит в Spotify вручную |

### MVP-2 Success (M5)

| Metric | Target | Как измеряем |
|--------|--------|-------------|
| AI fact engagement | ≥50% треков — кто-то читает факты | Click/expand events |
| Rating adoption | ≥2 участника рейтят ≥50% треков | DB: ratings per session |
| AI chat usage | ≥5 вопросов за сессию | Bot message count |
| Stats page visits | Участники заходят между сессиями | Mini App analytics |

---

## Pre-Session Checklist (Wednesday Protocol)

Выполнять за **1 час до сессии** (13:00 MSK):

- [ ] Spotify token valid (test API call)
- [ ] Bot process running (health check endpoint)
- [ ] Database accessible (connection test)
- [ ] WebSocket server accepting connections
- [ ] Mini App loads in Telegram (manual check)
- [ ] Current TURDOM playlist registered in bot
- [ ] All participants registered (Telegram ↔ Spotify mapping)

Если любой пункт fail → escalate to Nikita, fallback plan = ручное голосование.

---

## Rollback Plan

Если бот падает **во время сессии**:

1. Nikita объявляет в Discord: "бот упал, голосуем голосом"
2. Продолжаем сессию в ручном режиме
3. После сессии: debug logs → fix → redeploy
4. Голоса из ручного режима **не** вносятся в бот ретроспективно

---

## Решения для CEO

1. **Milestone timeline** — оценки по срокам давать не буду (PRD scope ещё уточняется), но порядок milestone'ов фиксирован
2. **Pre-session checklist** — автоматизировать в MVP-1 (health check endpoint)?
3. **Rollback protocol** — устраивает fallback к ручному режиму?
4. **Success metrics** — какие ещё метрики важны для тебя?
