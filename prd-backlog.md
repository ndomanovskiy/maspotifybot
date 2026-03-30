# PRD Improvement Backlog — MaSpotifyBot

> Сформирован на основе ревью всех департаментов (prd-review-checklist.md)
> Приоритизация: P0 = блокер MVP, P1 = нужно до первой сессии, P2 = post-MVP
> Дата: 2026-03-30

---

## P0 — Блокеры MVP (без этого нельзя начинать разработку)

### 1. Определить MVP-0 scope
**Источник:** Planning
**Проблема:** 47 user stories для первого релиза — нереально. Нет чёткой границы MVP.
**Действие:** Выделить MVP-0: session monitoring + voting + track removal. Всё остальное — phased rollout.
**Предложенная разбивка:**
- **MVP-0**: User Stories #1-3, #7, #9-11, #35 (session + voting + базовый маппинг)
- **MVP-1**: + #27-30 (дубликаты) + #32-33 (рекап) + #38-39 (менеджмент плейлистов)
- **MVP-2**: + #15-18 (рейтинги) + #19-21 (AI-факты) + #22-26 (AI-диалог)
- **Full**: + #41-47 (настройки, статистика, лидерборды)
**Owner:** Planning → CEO approve
**Статус:** 🔴 Открыт

### 2. Error handling & failure modes
**Источник:** Engineering
**Проблема:** PRD не описывает поведение при: падении Spotify API, потере WebSocket, таймауте AI, протухшем токене.
**Действие:** Добавить секцию "Failure Modes & Recovery" в PRD:
- Spotify API недоступен → показать "Spotify connection lost", retry 3x, fallback уведомление в чат
- WebSocket disconnect → auto-reconnect с exponential backoff, показать состояние в UI
- AI timeout → показать "Generating..." с таймаутом 10s, fallback "Facts unavailable"
- Session-critical failure → уведомить хоста, предложить restart/manual mode
**Owner:** Engineering
**Статус:** 🔴 Открыт

### 3. Auth flow & token management
**Источник:** Engineering + DevOps
**Проблема:** "Nikita's auth token" — и всё. Нет стратегии хранения, refresh, ротации.
**Действие:** Описать в PRD:
- OAuth2 PKCE flow для получения initial tokens
- Refresh token strategy: auto-refresh при 401, stored encrypted
- Fallback: если refresh token тоже протух → уведомление Nikita с re-auth link
- Хранение: encrypted в PostgreSQL или environment variable (NOT plaintext .env)
**Owner:** Engineering + DevOps
**Статус:** 🔴 Открыт

### 4. Secrets management
**Источник:** DevOps
**Проблема:** Spotify tokens, AI API keys, Telegram bot token — нигде не описано как хранить.
**Действие:** Определить:
- Environment variables через Docker secrets или .env с restricted permissions
- Никаких секретов в git
- Rotation policy для API keys
**Owner:** DevOps
**Статус:** 🔴 Открыт

---

## P1 — Нужно до первой live-сессии

### 5. WebSocket protocol spec
**Источник:** Engineering + Design
**Проблема:** Упомянут WebSocket, но нет event types, payload format, reconnection strategy.
**Действие:** Определить контракт:
- Events: `track_changed`, `vote_cast`, `vote_result`, `session_started`, `session_ended`, `track_removed`
- Payload format: JSON с типизацией
- Reconnection: exponential backoff, state sync on reconnect
**Owner:** Engineering
**Статус:** 🟡 Открыт

### 6. Data model (ER-диаграмма)
**Источник:** Engineering
**Проблема:** Текстовое описание таблиц недостаточно для 10 модулей.
**Действие:** Создать формальную ER-диаграмму: users, tracks, playlists, sessions, votes, ratings, session_events, duplicate_records
**Owner:** Engineering
**Статус:** 🟡 Открыт

### 7. API contract (OpenAPI spec)
**Источник:** Engineering
**Проблема:** Mini App ↔ Backend коммуникация не специфицирована.
**Действие:** Написать OpenAPI 3.0 spec для всех endpoints Mini App
**Owner:** Engineering
**Статус:** 🟡 Открыт

### 8. Voting concurrency & race conditions
**Источник:** Engineering + QA
**Проблема:** Одновременные голоса могут создать race condition на threshold check.
**Действие:** Определить в PRD: atomic vote counting (database-level lock или optimistic concurrency), idempotent vote submission
**Owner:** Engineering
**Статус:** 🟡 Открыт

### 9. CI/CD pipeline
**Источник:** DevOps
**Проблема:** Не описан вообще.
**Действие:** Определить: GitHub Actions → lint + test → Docker build → deploy to Selectel → healthcheck
**Owner:** DevOps
**Статус:** 🟡 Открыт

### 10. NFRs (Non-Functional Requirements)
**Источник:** QA
**Проблема:** Нет performance targets.
**Действие:** Добавить в PRD:
- Vote → track removal: < 2 sec
- AI fact generation: < 10 sec (first load), cached thereafter
- Mini App load time: < 3 sec
- WebSocket latency: < 500ms
- Uptime target: 99% (с учётом что critical только по средам 14:00-16:00 MSK)
**Owner:** QA + Engineering
**Статус:** 🟡 Открыт

---

## P2 — Post-MVP улучшения

### 11. Design system & tokens
**Источник:** Design
**Проблема:** "Stylish and polished" — это не спецификация.
**Действие:** Цветовая палитра, типографика, spacing, компонентная библиотека, design tokens в CSS variables. Figma prototype.
**Owner:** Design
**Статус:** ⚪ Отложен

### 12. Rating UX переосмысление
**Источник:** Design
**Проблема:** 5 слайдеров 1-10 на мобильном — UX-кошмар.
**Действие:** UX research: tap-to-rate, simplified scale (1-5), swipe cards, или progressive disclosure.
**Owner:** Design
**Статус:** ⚪ Отложен (не в MVP-0)

### 13. Monitoring & alerting stack
**Источник:** DevOps
**Проблема:** Нет monitoring requirements.
**Действие:** Минимум: healthcheck endpoint + uptime monitoring + Telegram alert on failure.
**Owner:** DevOps
**Статус:** ⚪ Отложен

### 14. Backup strategy
**Источник:** DevOps
**Проблема:** PostgreSQL без backup plan.
**Действие:** pg_dump → Selectel Object Storage, daily, 30-day retention.
**Owner:** DevOps
**Статус:** ⚪ Отложен

### 15. Test environment & strategy
**Источник:** QA
**Проблема:** Нет описания test environment, no acceptance criteria на user stories.
**Действие:** docker-compose.test.yml, VCR pattern для Spotify API, test matrix.
**Owner:** QA
**Статус:** ⚪ Отложен

---

## Сводка

| Приоритет | Кол-во | Статус |
|-----------|--------|--------|
| P0 (блокеры MVP) | 4 | 🔴 Все открыты |
| P1 (до первой сессии) | 6 | 🟡 Все открыты |
| P2 (post-MVP) | 5 | ⚪ Отложены |
| **Итого** | **15** | — |

---

## Следующий шаг

**Немедленно:** CEO утверждает MVP-0 scope (backlog item #1), после чего Engineering может начинать работу над P0 items параллельно.
