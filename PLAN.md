# Veyra Bot — Full Automation App

## Project Location

**New directory: `~/Desktop/veyra-bot/`** — clean start, old prototype stays as reference.

## Context

You have a working prototype (`slasher_app.py` + `slasher.py` + Chrome extension) that automates wave battling via direct HTTP POST to the game's PHP endpoints. You want to turn this into a production-grade, multi-account, fully automated app that can be hosted 24/7.

The game API is simple — cookie-based auth, POST endpoints for all actions, HTML scraping for monster data. All community tools (GonBruck/DemonGame, asura-cr/ui-addon, ShadmanSakib22's scripts) use the same approach. No browser automation needed.

## Language Decision

**Python first** (FastAPI + httpx + SQLAlchemy async), structured for future Go rewrite:
- Pure dataclasses for game types (map directly to Go structs)
- Thin HTTP client layer (one method = one API call)
- Pure parsing functions (string in, typed data out)
- Static frontend that talks to REST API + WebSocket (backend-agnostic)

## Hosting Recommendation

**VPS (DigitalOcean $6/mo or Hetzner $4/mo)** — best for a 24/7 bot. Platform-as-a-service (Railway/Render) free tiers sleep after inactivity. Docker makes deployment trivial anywhere.

## Tech Stack

- **FastAPI** — async, WebSocket support, auto-docs (better than Flask for multi-account concurrency)
- **httpx** — async HTTP client (replaces `requests`)
- **SQLAlchemy async + aiosqlite** — persistence (trivially migrates to PostgreSQL)
- **Alembic** — DB migrations
- **Fernet (cryptography)** — encrypt stored passwords
- **Pydantic Settings** — env-based config
- **Docker** — deployment

## Project Structure

```
veyra-bot/
├── src/veyra/
│   ├── main.py                   # FastAPI app, lifespan events
│   ├── config.py                 # Pydantic Settings (.env based)
│   ├── security.py               # Fernet encrypt/decrypt for passwords
│   ├── db/
│   │   ├── engine.py             # async SQLAlchemy engine
│   │   ├── models.py             # accounts, task_configs, attack_log, damage_tracker, session_stats
│   │   └── repositories.py      # Data access layer
│   ├── game/                     # Pure I/O layer — one GameClient per account
│   │   ├── client.py             # Async httpx wrapper for all game endpoints
│   │   ├── auth.py               # Login flow (ported from slasher_app.py:86-114)
│   │   ├── parser.py             # HTML/JSON parsing (ported from slasher_app.py:117-286)
│   │   ├── endpoints.py          # URL constants, stamina mappings
│   │   └── types.py              # Dataclasses: Monster, AttackResult, LootResult, etc.
│   ├── engine/                   # Bot logic — stateful, per-account
│   │   ├── account_manager.py    # AccountWorker lifecycle, multi-account orchestration
│   │   ├── rate_limiter.py       # Adaptive per-account rate limiter
│   │   ├── wave_farmer.py        # Port of worker() + farm_monster() from slasher_app.py
│   │   ├── pvp_bot.py            # Matchmaking, MP-based skill selection
│   │   ├── loot_collector.py     # Auto-claim from waves + dungeons
│   │   ├── stamina_farmer.py     # Manga chapter reactions for energy
│   │   └── dungeon_runner.py     # Guild dungeon participation
│   ├── api/                      # FastAPI routes
│   │   ├── accounts.py           # CRUD accounts
│   │   ├── tasks.py              # Start/stop/configure tasks per account
│   │   ├── dashboard.py          # Aggregated stats
│   │   ├── logs.py               # WebSocket real-time logs
│   │   └── profiles.py           # Targeting profiles
│   └── web/                      # Static frontend (SPA)
│       ├── index.html
│       ├── app.js
│       └── style.css
├── alembic/                      # DB migrations
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Database Schema

- **accounts** — id, name, email, password_enc (Fernet), game_user_id, session_cookies, is_active
- **task_configs** — account_id FK, task_type (wave_farm|pvp|loot|stamina_farm|dungeon), config_json, enabled
- **damage_tracker** — (account_id, monster_id) PK, damage_dealt, updated_at
- **attack_log** — account_id, monster_id, monster_name, wave, damage_dealt, stamina_spent, result, created_at
- **session_stats** — account_id, task_type, started_at, ended_at, monsters_attacked, total_damage, kills, loot_collected, pvp_wins/losses

## Multi-Account Architecture

- One `AccountWorker` per active account, each with its own:
  - `httpx.AsyncClient` (independent cookie jar)
  - `RateLimiter` (so Account A being throttled doesn't affect Account B)
  - `GameClient` instance
- `AccountManager` orchestrates all workers via `asyncio.Task`
- Tasks within one account share the same rate limiter (safe request rate)

## Implementation Phases

### Phase 1: Scaffolding + Game Client (first)
- Project structure, pyproject.toml, config, security, DB models, Alembic migration
- Port `game/types.py`, `game/endpoints.py`, `game/auth.py`, `game/parser.py`
- Build `game/client.py` async wrapper
- Unit tests for parser using HTML/JSON fixtures

### Phase 2: Wave Farming Engine (core feature)
- `engine/rate_limiter.py` — adaptive delay
- `engine/wave_farmer.py` — direct port of slasher_app.py worker/farm_monster
- `engine/account_manager.py` — single account first
- `engine/loot_collector.py` — basic auto-loot on monster kill
- `db/repositories.py` — data access for accounts, damage tracker, logs

### Phase 3: API + Dashboard
- All FastAPI routes (accounts CRUD, task control, stats, profiles)
- WebSocket log streaming (replaces SSE)
- Basic web dashboard: account list, start/stop, real-time logs, stats
- Profile management (backward-compatible with existing profiles.json)

### Phase 4: Multi-Account
- Extend AccountManager for concurrent AccountWorkers
- Per-account dashboard tabs
- Account add/remove/edit UI

### Phase 5: PvP Bot
- `pvp_bot.py` — matchmaking, skill selection (Power Slash 9MP > Back Stab 4MP > Slash free)
- PvP stats tracking

### Phase 6: Stamina Farming
- Reverse-engineer manga chapter reaction endpoints
- `stamina_farmer.py` — auto-trigger when stamina runs out

### Phase 7: Guild Dungeons
- `dungeon_runner.py` — participation + loot collection

### Phase 8: Production Hardening + Docker
- Structured logging, health checks, graceful shutdown
- Session persistence/auto-refresh
- Docker multi-stage build, docker-compose with volume mounts

## Key Code Ports (source -> destination)

| Existing Code | Port To |
|---|---|
| `slasher_app.py:86-114` (do_login) | `game/auth.py` |
| `slasher_app.py:117-201` (fetch_monsters) | `game/parser.py::parse_monsters` |
| `slasher_app.py:242-286` (parse_damage) | `game/parser.py::parse_damage_response` |
| `slasher_app.py:330-419` (farm_monster) | `engine/wave_farmer.py` |
| `slasher_app.py:425-546` (worker) | `engine/wave_farmer.py::run` |
| `slasher.py:323-360` (get_pending) | `engine/wave_farmer.py::_fetch_and_filter` |
| `slasher_app.py:70-80` (step_down_stamina) | `game/endpoints.py::STAMINA_STEP_DOWN` |
| `profiles.json` format | `api/profiles.py` (backward compatible import) |

## Verification Plan

1. **Phase 1**: Run parser unit tests against saved HTML fixtures. Test login against live game.
2. **Phase 2**: Start wave farming for one account, verify damage tracking matches existing slasher_app behavior.
3. **Phase 3**: Open dashboard in browser, verify real-time logs and stats update via WebSocket.
4. **Phase 4**: Run 2 accounts simultaneously, verify independent sessions and rate limiting.
5. **Phase 8**: `docker compose up` and verify the app runs, persists data across restarts.
