# MaSpotifyBot — User Stories MoSCoW Prioritization

> Version: 1.0
> Date: 2026-03-30
> Author: Obi-Wan (Planning)

---

## Must Have (без этого бот бесполезен)

| # | Story (краткое) | Модуль | MVP |
|---|----------------|--------|-----|
| 1 | Start session | Session Manager | 0 |
| 2 | Session start notification | Bot | 0 |
| 3 | See current track in Mini App | Frontend | 0 |
| 7 | Detect pause/resume/skip | Spotify Monitor | 0 |
| 8 | End session | Session Manager | 0 |
| 9 | Vote keep/drop | Vote Engine | 0 |
| 10 | Auto-remove on threshold | Vote Engine | 0 |
| 11 | Skip after drop | Vote Engine | 0 |
| 13 | Adaptive threshold | Vote Engine | 0 |
| 35 | Register participants | User Registry | 0 |

**10 stories — ядро продукта.**

## Should Have (сильно улучшают опыт)

| # | Story (краткое) | Модуль | MVP |
|---|----------------|--------|-----|
| 4 | Avatar who added track | Frontend | 1 |
| 5 | Playback timer | Frontend | 1 |
| 6 | Session name in UI | Frontend | 1 |
| 12 | Drop notification | Frontend | 0 |
| 14 | Configurable threshold | Admin | 1 |
| 27 | Monitor playlist for new tracks | Duplicate Detector | 1 |
| 28 | DM alert on duplicate | Duplicate Detector | 1 |
| 29 | Cross-playlist duplicate check | Duplicate Detector | 1 |
| 32 | Session recap | Session Manager | 1 |
| 33 | Recap content (stats, genres) | Session Manager | 1 |
| 36 | Onboarding flow | User Registry | 1 |
| 37 | Manage participant list | Admin | 0 |
| 38 | Archive playlist after session | Playlist Manager | 1 |

**13 stories — делают бот полноценным.**

## Could Have (приятно, но можно без)

| # | Story (краткое) | Модуль | MVP |
|---|----------------|--------|-----|
| 15 | Rate on 5 criteria | Rating Engine | 2 |
| 16 | Rating criteria definition | Rating Engine | 2 |
| 17 | Quick rating UI | Frontend | 2 |
| 18 | Rate after session | Rating Engine | 2 |
| 19 | AI track facts | AI Service | 2 |
| 20 | "Tell me more" button | AI Service | 2 |
| 22 | Free-form AI questions | AI Service | 2 |
| 23 | "Was this track in playlist?" | AI Service | 2 |
| 30 | ISRC duplicate detection | Duplicate Detector | 1 |
| 31 | Fuzzy duplicate matching | Duplicate Detector | 1 |
| 34 | Visual recap (not just text) | Session Manager | 1 |
| 39 | Register new playlists | Playlist Manager | 1 |
| 40 | Import 2649 tracks | Playlist Manager | 2 |
| 44 | Browse session history | Frontend | 2 |

**14 stories — обогащают продукт.**

## Won't Have (сейчас нет, потом может)

| # | Story (краткое) | Модуль | MVP |
|---|----------------|--------|-----|
| 21 | Lyrics button | AI Service | 2+ |
| 24 | "Who adds most rock?" stat query | AI Service | 2+ |
| 25 | "Tell me about artist" query | AI Service | 2+ |
| 26 | Multi-language response | AI Service | 2+ |
| 41 | Light/dark theme toggle | Frontend | 2 |
| 42 | Language preference | Settings | 2 |
| 43 | Admin settings in Mini App | Frontend | 2 |
| 45 | Personal stats page | Frontend | 2 |
| 46 | Group leaderboards | Frontend | 2 |
| 47 | Browse all TURDOM playlists | Frontend | 2 |

**10 stories — отложены.**

---

## Summary

| Priority | Count | % |
|----------|-------|---|
| Must Have | 10 | 21% |
| Should Have | 13 | 28% |
| Could Have | 14 | 30% |
| Won't Have (now) | 10 | 21% |
| **Total** | **47** | **100%** |

MVP-0 покрывает все Must Have + 2 Should Have = **12 stories**.
