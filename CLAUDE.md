# Veyra Bot — Claude Code Context

## What is this project?

Veyra Bot is a single-account automation bot for the browser game on **demonicscans.org**. It runs as a FastAPI web server with a vanilla JS frontend, providing automated wave farming, PvP auto-fighting, stamina farming, stat allocation, and loot management.

**Stack:** Python 3.12, FastAPI, httpx, BeautifulSoup4, SQLAlchemy (async SQLite), vanilla HTML/CSS/JS frontend.

**Deployment:** Hetzner CX22 (Helsinki), deployed via `scp` + systemd service.

---

## Security Rules

### NEVER do these:
- **Never hardcode credentials, API keys, passwords, or Fernet keys** in source code.
- **Never commit `.env`** — it contains real email/password/keys. It is already in `.gitignore`.
- **Never commit `veyra.db`** — it may contain encrypted passwords. Already in `.gitignore`.
- **Never log or print plaintext passwords** — the login flow receives passwords in memory only.
- **Never expose the `VEYRA_FERNET_KEY`** — it encrypts stored account passwords.
- **Never add `profiles.json` to git** — it stores user-defined target profiles (non-sensitive but local).
- **Never remove entries from `.gitignore`** without explicit approval.
- **Never weaken the DocsGuard middleware** — `/docs`, `/redoc`, `/openapi.json` must stay key-protected.

### Sensitive files (DO NOT commit, share, or log contents of):
| File | Contains |
|------|----------|
| `.env` | `VEYRA_DEV_EMAIL`, `VEYRA_DEV_PASSWORD`, `VEYRA_DOCS_KEY`, `VEYRA_FERNET_KEY` |
| `veyra.db` | SQLite DB with Fernet-encrypted passwords in `accounts.password_enc` |
| `profiles.json` | User farming profiles (local config) |
| `loot_db.json` | Scraped monster loot tables (not sensitive, but large — 37KB) |

### When modifying code:
- Keep credentials flowing through `veyra/config.py` (`Settings` with `env_prefix="VEYRA_"`) — never read `.env` directly.
- The `veyra/security.py` module handles Fernet encrypt/decrypt — do not bypass it for password storage.
- Session tokens are generated via `secrets.token_hex(32)` — do not weaken this.
- The `DocsGuard` middleware in `main.py` protects Swagger UI behind `VEYRA_DOCS_KEY` — keep this.

---

## Project Structure

```
veyra-bot/
├── .env                    # Secrets (gitignored)
├── .env.example            # Template showing required env vars
├── .gitignore              # Excludes .env, *.db, .venv, __pycache__
├── pyproject.toml          # Dependencies and build config (hatchling)
├── Dockerfile              # Python 3.12-slim, exposes port 5678
├── docker-compose.yml      # Single service with persistent volume
├── START.sh                # Local dev: activate venv + run
├── PLAN.md                 # Original design document
├── alembic/                # DB migrations (alembic)
├── alembic.ini             # Alembic config
├── profiles.json           # Saved farming profiles (local)
├── loot_db.json            # Cached monster loot tables (JSON)
├── veyra.db                # SQLite database (gitignored)
├── tests/                  # Test directory (mostly empty)
│
├── veyra/                  # Main Python package
│   ├── __init__.py
│   ├── config.py           # Pydantic Settings — reads all VEYRA_* env vars
│   ├── security.py         # Fernet encrypt/decrypt for stored passwords
│   ├── main.py             # FastAPI app, lifespan, DocsGuard middleware, static files
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   └── routes.py       # All REST endpoints: connect, start/stop, status, logs (SSE),
│   │                       #   profiles CRUD, loot queries, PvP, stat allocator
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py       # SQLAlchemy async engine + session factory
│   │   ├── models.py       # ORM models: Account, TaskConfig, DamageTracker, AttackLog, SessionStats
│   │   └── repositories.py # CRUD repos for all models (AccountRepo, etc.)
│   │
│   ├── engine/             # Core automation logic
│   │   ├── __init__.py
│   │   ├── account_manager.py  # Singleton AccountManager — orchestrates connect/start/stop
│   │   │                       #   for farming, PvP, and stat allocation workers
│   │   ├── wave_farmer.py      # Main farming loop: fetch waves, attack targets by priority,
│   │   │                       #   stamina step-down, auto-rejoin, smart loot on exhaustion
│   │   ├── pvp_fighter.py      # PvP auto-fight: matchmake, set auto-play, poll until done
│   │   ├── stat_allocator.py   # Auto stat point allocation (rounds to multiples of 10)
│   │   ├── stamina_farmer.py   # Background manga reaction loop for +2 stamina/reaction
│   │   ├── loot_collector.py   # Smart loot: calculates EXP needed, loots just enough to level up
│   │   ├── loot_database.py    # JSON-backed monster loot cache, scrapes via dev account
│   │   └── rate_limiter.py     # Adaptive rate limiter (backs off on 429, recovers on success)
│   │
│   ├── game/               # Game HTTP client and parsing
│   │   ├── __init__.py
│   │   ├── auth.py         # Login flow: parse form, fill credentials, POST, verify success
│   │   ├── client.py       # GameClient: async httpx wrapper for all game endpoints
│   │   │                   #   (attack, join, loot, PvP, stats, reactions, potions, etc.)
│   │   ├── endpoints.py    # All game URLs, stamina options, headers, wave map
│   │   ├── parser.py       # BeautifulSoup HTML parsing: monsters, loot, stats, chapters, PvP tokens
│   │   └── types.py        # Dataclasses: Monster, MonsterGroup, AttackResult, TargetConfig,
│   │                       #   PlayerStats, CharacterStats, LootItem, StaminaPotion, etc.
│   │
│   └── web/                # Frontend (vanilla, no build step)
│       ├── index.html      # Single-page dashboard
│       ├── app.js          # Frontend logic: login, target config, start/stop, SSE logs
│       └── style.css       # Dark theme styling
```

---

## Key Architectural Patterns

- **Single-account singleton:** `AccountManager` in `account_manager.py` manages one connected account at a time. Multiple workers (farming, PvP, stats) run as concurrent `asyncio.Task`s.
- **Site-down resilience:** `GameClient` tracks consecutive network failures. After 3 failures, it enters "site down" mode and polls every 60s until recovery. All workers respect this.
- **Stamina conservation:** The farming loop steps down stamina cost (200 -> 100 -> 50 -> 10 -> 1) on exhaustion before stopping. Background reaction farming tops up stamina concurrently.
- **Smart looting:** `loot_collector.py` calculates the minimum number of corpses to loot for a level-up (restoring stamina), saving remaining corpses for future level-ups.
- **SSE logs:** Real-time log streaming via Server-Sent Events at `GET /api/logs`.

---

## Running Locally

```bash
source .venv/bin/activate
python -m veyra.main
# or: ./START.sh
```

Server starts at `http://127.0.0.1:5678`. Swagger docs at `/docs?key=YOUR_DOCS_KEY`.

---

## Common Tasks

- **Add a new game endpoint:** Add URL to `endpoints.py`, HTTP method to `client.py`, parser to `parser.py`, expose via `routes.py`.
- **Add a new automation worker:** Create in `engine/`, add state dataclass, wire into `AccountManager`, add API routes.
- **Modify farming behavior:** Edit `wave_farmer.py` (attack loop) or `stamina_farmer.py` (reaction loop).
- **Change frontend:** Edit `veyra/web/` files directly (no build step).
