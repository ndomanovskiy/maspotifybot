# PRD Review Checklist — MaSpotifyBot

> Документ для структурированного ревью PRD (02 - PRD.md) всеми департаментами.
> Каждый лид заполняет свою секцию: gaps, risks, suggestions.
> Deadline: 24 часа с момента получения.

---

## Engineering

### Gaps (что упущено)
- [ ] **Error handling strategy** — PRD не описывает поведение при падении Spotify API, потере WebSocket-соединения, таймаутах AI-сервиса. Нужна секция "Failure Modes & Recovery".
- [ ] **Data model / schema** — есть текстовое описание таблиц, но нет ER-диаграммы или формального schema definition. Для 10 модулей это критично.
- [ ] **Auth flow details** — описан только "Nikita's auth token" для Spotify. Как хранится? Refresh token strategy? Что если токен протухнет во время сессии?
- [ ] **WebSocket protocol** — упомянут WebSocket для real-time, но нет описания событий (event types, payload format, reconnection strategy).
- [ ] **Migration strategy** — импорт 2649 треков описан поверхностно. Нужен план: batch size, error handling, progress tracking, rollback.
- [ ] **API rate limiting** — "exponential backoff on 429" недостаточно. Нужны конкретные лимиты на polling frequency, concurrent requests, queue strategy.
- [ ] **Concurrency** — что происходит при одновременных голосах? Race condition на threshold check?

### Risks
- [ ] **Single point of failure** — всё завязано на токен одного пользователя (Nikita). Если токен протухнет = полная остановка.
- [ ] **Spotify polling at 3-5 sec** — при 2-часовой сессии = 1440-2400 API calls. Риск rate limiting.
- [ ] **tekore maintenance** — библиотека менее популярна чем spotipy. Проверить активность поддержки.
- [ ] **Monolith scaling** — при 4-5 юзерах ок, но если группа вырастет — polling + WebSocket + AI calls в одном процессе.

### Suggestions
- [ ] Добавить секцию "System Architecture Diagram" (C4 level 2 минимум).
- [ ] Определить API contract (OpenAPI spec) для Mini App ↔ Backend.
- [ ] Добавить health check / monitoring requirements.
- [ ] Рассмотреть spotipy как альтернативу tekore (больше community, лучше docs).
- [ ] Описать caching strategy для AI-фактов (не генерить заново для повторных треков).

---

## Design / Frontend

### Gaps
- [ ] **Design system** — "stylish and visually polished" это вайб, не спецификация. Нужны: цветовая палитра, типографика, spacing system, компонентная библиотека.
- [ ] **Responsive breakpoints** — "mobile-first" упомянуто, но Mini App может открываться и на десктопе. Какие breakpoints?
- [ ] **Animation / transitions** — переход между треками, появление результатов голосования, notification bar — нужны specs.
- [ ] **Accessibility** — нет упоминания a11y. Минимум: contrast ratios, touch target sizes, screen reader support.
- [ ] **Offline state** — что показывать если потеряна связь с сервером?
- [ ] **Loading states** — для AI-фактов, lyrics, voting results — какие skeleton/spinner?
- [ ] **Empty states** — первая сессия, нет истории, нет статистики — что показывать?

### Risks
- [ ] **Telegram Mini App limitations** — ограничения платформы (viewport, navigation, back button) могут конфликтовать с UI concept.
- [ ] **5 sliders for rating** — на мобильном экране 5 слайдеров 1-10 это UX-кошмар. Высокий риск что никто не будет рейтить.
- [ ] **Real-time sync визуально** — playback timer "synced with Spotify" при polling 3-5 sec будет дёргаться.

### Suggestions
- [ ] Провести UX research: 5 слайдеров заменить на tap-to-rate (звёздочки) или swipe cards.
- [ ] Создать Figma prototype до начала разработки.
- [ ] Определить design tokens (цвета, тени, радиусы) в CSS variables.
- [ ] Рассмотреть Telegram Mini App SDK ограничения и задокументировать их в PRD.
- [ ] Добавить micro-interactions spec (haptic feedback на vote, confetti на unanimous keep).

---

## QA / Testing

### Gaps
- [ ] **Test environment** — PRD говорит "mock external services", но нет описания test environment setup. Нужен docker-compose.test.yml?
- [ ] **E2E testing strategy** — Mini App тестируется как? Playwright? Manual? Telegram test environment?
- [ ] **Performance requirements** — нет NFRs. Какой допустимый latency для vote → track removal? Для AI fact generation?
- [ ] **Load testing** — даже для 4-5 юзеров, concurrent votes + WebSocket + Spotify polling = нужен baseline.
- [ ] **Regression strategy** — как тестировать что новые фичи не ломают voting logic?
- [ ] **Test data management** — 2649 треков для тестов? Subset? Fixtures?

### Risks
- [ ] **Non-deterministic AI** — PRD говорит "не unit-тестировать AI", но AI-факты могут содержать ошибки/галлюцинации. Кто и как верифицирует?
- [ ] **Spotify sandbox** — нет официального sandbox. Тесты против реального API = flaky tests + risk of modifying real playlists.
- [ ] **Voting race conditions** — одновременные голоса в тестах сложно воспроизвести детерминистически.

### Suggestions
- [ ] Определить acceptance criteria для каждой user story (сейчас их нет).
- [ ] Добавить NFRs: response time < X ms, uptime %, max concurrent sessions.
- [ ] Создать test matrix: manual vs automated, unit vs integration vs e2e.
- [ ] Для Spotify: использовать recorded HTTP fixtures (VCR pattern).
- [ ] Определить smoke test suite для pre-session health check.

---

## DevOps / Infrastructure

### Gaps
- [ ] **CI/CD pipeline** — не описан. GitHub Actions? Stages? Deploy strategy?
- [ ] **Monitoring & alerting** — нет requirements. Что мониторим? Куда алертим? Prometheus? Grafana?
- [ ] **Backup strategy** — PostgreSQL с голосами и рейтингами за 89+ сессий — нужен backup plan.
- [ ] **Secrets management** — Spotify tokens, AI API keys, Telegram bot token — как хранятся? .env? Vault?
- [ ] **Logging** — structured logging? Log aggregation? Уровни логирования?
- [ ] **SSL/TLS** — Mini App требует HTTPS. Cert management?
- [ ] **Domain / DNS** — нужен домен для Mini App endpoint.

### Risks
- [ ] **Single VM** — Selectel VM как единственный хостинг = single point of failure. Нет redundancy.
- [ ] **Docker on VM** — без orchestration (K8s) ручной restart при падении. Нужен хотя бы docker restart policy + healthcheck.
- [ ] **Database on same VM** — PostgreSQL на том же VM что и app = shared resources, risk of disk full.

### Suggestions
- [ ] Добавить секцию "Infrastructure Requirements" в PRD.
- [ ] Определить deploy pipeline: push → test → build → deploy → healthcheck.
- [ ] docker-compose.yml с healthchecks, restart policies, resource limits.
- [ ] Automated backups: pg_dump → S3-compatible storage (Selectel Object Storage).
- [ ] Определить monitoring stack: минимум Prometheus + Grafana или managed monitoring.
- [ ] Рассмотреть managed PostgreSQL (Selectel DBaaS) вместо self-hosted.

---

## Planning (мой анализ)

### Gaps
- [ ] **MVP scope не выделен** — PRD описывает полный продукт, но нет чёткой границы "что в MVP, что после". 47 user stories для первого релиза — это too much.
- [ ] **Priority / MoSCoW** — user stories не приоритизированы. Что must-have для первой сессии?
- [ ] **Timeline / milestones** — нет сроков. Когда MVP? Когда beta?
- [ ] **Success metrics** — как измеряем что бот полезен? Adoption rate? % tracks rated? Session satisfaction?
- [ ] **Rollback plan** — если бот сломается во время сессии — fallback to manual process?
- [ ] **Onboarding plan** — 4-5 человек, но всё равно нужен: как объясняем что делать, tutorial flow.

### Risks
- [ ] **Scope creep** — 47 stories + AI + real-time + Mini App = огромный scope для pet project. Риск заброса.
- [ ] **Wednesday dependency** — бот нужен по средам в 14:00. Если баг обнаружен в среду в 13:50 — нет времени чинить.
- [ ] **Single maintainer** — Nikita единственный разработчик + единственный Spotify auth holder. Bus factor = 1.

### Suggestions
- [ ] **Выделить MVP-0** (минимально жизнеспособный): session monitoring + voting + track removal. Без AI, без рейтингов, без статистики.
- [ ] Добавить MoSCoW приоритизацию к каждой user story.
- [ ] Определить milestones: MVP-0 → MVP-1 (+ duplicates + recap) → MVP-2 (+ AI + ratings) → Full.
- [ ] Добавить success metrics: "бот используется 3 сессии подряд без fallback к ручному процессу".
- [ ] Wednesday pre-session checklist: health check бота за 1 час до сессии.

---

## Action Items

| # | Action | Owner | Deadline |
|---|--------|-------|----------|
| 1 | Engineering заполняет свою секцию | Engineering Lead | +24h |
| 2 | Design заполняет свою секцию | Design Lead | +24h |
| 3 | QA заполняет свою секцию | QA Lead | +24h |
| 4 | DevOps заполняет свою секцию | DevOps Lead | +24h |
| 5 | Planning консолидирует и обновляет PRD | Obi-Wan (Planning) | +48h |
| 6 | MVP-0 scope definition | All leads, facilitated by Planning | +48h |
| 7 | Updated PRD v1.1 published | Obi-Wan | +72h |

---

> **Процесс**: каждый лид ревьюит PRD через призму своей экспертизы, добавляет items в свою секцию. После сбора всех inputs — Planning консолидирует и обновляет PRD до v1.1.
