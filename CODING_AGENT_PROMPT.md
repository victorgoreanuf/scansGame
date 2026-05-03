# Coding Agent Prompt — Guild Dungeon PvP Worker

Hand this entire file to a fresh coding agent. It assumes the agent has access to the repo at `/Users/goreanuvictor/Desktop/scansGame` and can read/write files there.

---

## Goal

Add a 24/7 background worker to the Veyra Bot that automatically joins guild-dungeon **PvP-style** matches inside *The Polyhedral Crucible* (a "cube" dungeon under the new **Guild** tab — separate from the existing Adventure Guild). Surface it in the UI with a Start/Stop button and a status line, matching the patterns already used by the other workers (Wave Farmer, PvP Fighter, Stamina Farmer, Stat Allocator).

The full design — endpoints, picker algorithm, scheduling rules, edge cases — is already written up in [`GUILD_DUNGEON_PLAN.md`](GUILD_DUNGEON_PLAN.md) at the project root. **Read that file first and treat it as the spec.** Everything below is a build checklist on top of it.

---

## What's already verified (so the agent doesn't re-discover it)

The endpoints and contracts in the plan have been probed end-to-end against a live dungeon instance. Specifically, this exact join flow has succeeded:

1. `GET /guild_dash.php` → parse Open Dungeons cards → find `The Polyhedral Crucible` instance.
2. `GET /guild_dungeon_cube.php?instance_id={iid}` → extract inline `const STATE = {...}` JSON.
3. `POST /guild_dungeon_cube_action.php` `action=enter_node, instance_id, node_id, face_key` → returns `{ok, redirect, state}`.
4. `GET /pvp_style_node.php?source=cube&instance_id={iid}&node_id={nid}` → 20 `<div class="match">` cards, each with `Match #N / Slots X/5 STATUS_LABEL` plus a `pvp_style_battle.php?...&match_no=N` link.
5. `GET /pvp_style_state.php?source=cube&instance_id={iid}&node_id={nid}&match_no={N}` (Referer `pvp_style_battle.php?...`) → `{room_status, room_joined, room_slot, match: {ended, winner_side}, teams: {ally: {players_by_num: {1..5}}}, ...}`.
6. `POST /pvp_style_action.php` `source=cube, instance_id, node_id, match_no, action=pick_slot, slot_index={1..5}` → joins. Returns `{ok: true, message: "Formation filled. The match is now live."}` if our slot was the 5th.

Standalone working probe scripts (read-only browsing for inspiration / reference): `/tmp/probe_guild.py`, `/tmp/probe_cube.py`, `/tmp/probe_join.py`, `/tmp/probe_verify.py`. They are NOT production code — do not import from them — but they show the exact request shapes that work.

**Cooldown is the tricky bit:** there is **no JSON field** for the per-player cooldown. It surfaces only as plain text on `/pvp_style_node.php` after a match has resolved:

> `You are currently locked to another match in this node for about HH:MM:SS more.`

Regex: `r"locked to another match in this node for about (\d+):(\d{2}):(\d{2}) more"`. The cooldown is ~2h from resolution time and is **per node** (Ring Ward and Duel Heart will have independent cooldowns once Duel Heart unlocks).

---

## What to build

Follow the existing project patterns. Look at these files first to see how the other workers are structured:

- [`veyra/engine/pvp_fighter.py`](veyra/engine/pvp_fighter.py) — closest analog (state dataclass, async loop, log lines, stop check, site-down handling)
- [`veyra/engine/wave_farmer.py`](veyra/engine/wave_farmer.py) — for the timing/sleep patterns
- [`veyra/engine/account_manager.py`](veyra/engine/account_manager.py) — singleton orchestrator; the new worker plugs in here
- [`veyra/api/routes.py`](veyra/api/routes.py) — every worker has start/stop/status endpoints; copy the shape exactly
- [`veyra/web/app.js`](veyra/web/app.js) and [`veyra/web/index.html`](veyra/web/index.html) — UI sections per worker; mirror the structure of an existing one

### Files to add or modify

#### 1. `veyra/game/endpoints.py` — add URLs

```python
# Guild Dungeon (cube)
GUILD_DUNGEON_DASH_URL        = f"{BASE_URL}/guild_dash.php"          # (already? — check; if not, add it)
GUILD_DUNGEON_CUBE_URL        = f"{BASE_URL}/guild_dungeon_cube.php"
GUILD_DUNGEON_CUBE_ACTION_URL = f"{BASE_URL}/guild_dungeon_cube_action.php"

# PvP-style rooms (under the cube)
PVP_STYLE_NODE_URL    = f"{BASE_URL}/pvp_style_node.php"
PVP_STYLE_STATE_URL   = f"{BASE_URL}/pvp_style_state.php"
PVP_STYLE_BATTLE_URL  = f"{BASE_URL}/pvp_style_battle.php"
PVP_STYLE_ACTION_URL  = f"{BASE_URL}/pvp_style_action.php"
```

The dungeon name is fixed: `THE_POLYHEDRAL_CRUCIBLE_NAME = "The Polyhedral Crucible"`. The two PvP node `key` values to target are `"ring_ward"` and `"duel_heart"`. These are stable per dungeon definition (verified across two instances) but the worker MUST still re-discover their `node_id` / `face_key` by parsing `STATE.nodes[*].key` each cycle (do **not** hardcode 7/right and 8/right).

#### 2. `veyra/game/parser.py` — add parsers

```python
def parse_open_dungeons(html: str) -> list[dict]:
    """Parse the Open Dungeons section of guild_dash.php.
    Returns a list of {name: str, instance_id: str, dungeon_info_id: int|None}.
    """

def parse_cube_state(html: str) -> dict:
    """Extract the inline `const STATE = {...}` JSON from guild_dungeon_cube.php.
    Returns the parsed dict (with .nodes, .selected_node, .current_face_key, etc.).
    Raises ValueError if STATE not found.
    """

@dataclass
class PvpNodeMatchCard:
    match_no: int
    slots: int             # number of joined humans (0..5)
    cap: int               # always 5
    status: str            # "OPEN" | "LIVE" | "CLEARED"
    href: str              # pvp_style_battle.php?...&match_no=N

def parse_pvp_node_matches(html: str) -> list[PvpNodeMatchCard]:
    """Parse all <div class='match'> cards on pvp_style_node.php.
    Returns sorted by match_no.
    """

def parse_pvp_node_cooldown(html: str) -> int | None:
    """Look for the 'locked to another match in this node for about HH:MM:SS more'
    text on pvp_style_node.php. Returns remaining seconds, or None if not present.
    Uses regex: r"locked to another match in this node for about (\\d+):(\\d{2}):(\\d{2}) more"
    """
```

#### 3. `veyra/game/client.py` — add HTTP wrappers

Add methods on `GameClient` (don't break existing ones). Match the existing referer/content-type conventions seen in `pvp_set_auto`, `attack`, etc.

```python
async def fetch_open_dungeons(self) -> list[dict]: ...
async def fetch_cube_state(self, instance_id: str) -> dict: ...
async def enter_cube_node(self, instance_id: str, node_id: int|str, face_key: str) -> dict:
    """POST guild_dungeon_cube_action.php action=enter_node. Returns the parsed JSON
    (with redirect/state). Does not follow the redirect."""

async def fetch_pvp_node_matches(self, instance_id: str, node_id: int|str) -> tuple[list[PvpNodeMatchCard], int|None]:
    """GET pvp_style_node.php once. Returns (cards, cooldown_seconds_or_None)."""

async def fetch_pvp_match_state(self, instance_id: str, node_id: int|str, match_no: int) -> dict: ...

async def pvp_pick_slot(self, instance_id: str, node_id: int|str, match_no: int, slot_index: int) -> dict:
    """POST pvp_style_action.php action=pick_slot. Returns the parsed response."""
```

#### 4. `veyra/engine/dungeon_pvp_farmer.py` — NEW file

The worker. Mirror `pvp_fighter.py` in shape. Key points:

- **Target rooms (in order):** `ring_ward`, then `duel_heart`. Plus eventually `tyrant_conclave` (PvP boss in the Subjugation Undercroft, `face_key=bottom`). Implement all three — the picker code is identical, only the `key` differs. Process in order; one join per room per cycle.

- **Per-room picker (per Section 4 of the plan):**
  1. Look up the node by `key` in cube STATE. **Skip the room if** `node.status == 'hidden'` OR `node.is_cleared == 1`.
  2. POST `enter_node` (we don't need the redirect URL itself; the cube-state action just confirms entry).
  3. GET `pvp_style_node.php` once → parse all match cards AND the cooldown text.
  4. **If cooldown text is present** → record it, do not attempt to join in this room this cycle. Move on to next room.
  5. From cards, candidate set = `status == "OPEN" AND slots < 5`.
  6. **Preference:** highest `slots` in `[1..4]` (ties → lowest `match_no`); else lowest `match_no` with `slots == 0`. If none, do nothing for this room.
  7. GET `pvp_style_state.php` for the chosen match. **Belt-and-suspenders guard:** if `room_joined == True` for that match, skip the room entirely (we're already in another match in this node).
  8. From `state.teams.ally.players_by_num`, find an empty slot — for `slot in 1..5`, empty iff `ally.get(str(slot)) is None` (handles both literal `null` and missing keys; see Section 3.2 of the plan). Defensive fallback: also treat present-but-zeroed entries (`user_id == 0 AND npc_id == 0`) as empty.
  9. POST `pvp_style_action.php action=pick_slot`. Log success/failure.

- **Adaptive scheduler (per Section 4.1 of the plan):**
  - Default tick = **30 minutes**.
  - After each cycle, compute `next_wakeup_seconds`:
    - For every room that returned a cooldown, take `min(cooldown_seconds + 60)` across rooms.
    - If `min_cooldown < 30*60` → `next_wakeup_seconds = min_cooldown + 60`.
    - Otherwise → `next_wakeup_seconds = 30*60` (1800 s).
  - Sleep with stop checks every ~5 s (so Stop in the UI is responsive).
  - **Site-down behavior:** before each cycle, check `client.is_site_down`; if so, `await client.wait_for_site_up(log_fn, stop_check)` then continue.

- **State dataclass:**
  ```python
  @dataclass
  class DungeonPvpStatus:
      running: bool
      last_action_at: str | None  # ISO timestamp
      last_action: str | None     # human-readable, e.g. "joined Ring Ward match #12 slot 5"
      next_wake_at: str | None    # ISO timestamp
      cooldowns: dict[str, int]   # room_key -> seconds remaining (snapshot from last cycle)
      last_error: str | None
  ```

- **Logging:** use a per-account `log_fn` consistent with other workers (it pushes lines into the SSE log stream). Helpful lines:
  ```
  [dungeon-pvp] cycle start
  [dungeon-pvp] resolved instance_id=7135
  [dungeon-pvp] ring_ward: status=in_progress, scanning matches
  [dungeon-pvp] ring_ward: 18 OPEN, 2 LIVE; picker chose match #12 (4/5)
  [dungeon-pvp] ring_ward: joined slot 5 — "Formation filled. The match is now live."
  [dungeon-pvp] ring_ward: cooldown 01:56:31 remaining
  [dungeon-pvp] duel_heart: status=hidden, skipping
  [dungeon-pvp] sleeping 7050s (until 2026-05-04 01:14:00)
  ```

#### 5. `veyra/engine/account_manager.py` — wire it in

Mirror the existing pattern (look at how `start_pvp / stop_pvp / get_pvp_status` are done):

- Add `_dungeon_pvp_task: asyncio.Task | None = None` and `_dungeon_pvp_status: DungeonPvpStatus`.
- Methods: `start_dungeon_pvp()`, `stop_dungeon_pvp()`, `get_dungeon_pvp_status()`.
- Ensure the worker is properly cancelled on logout / disconnect.

#### 6. `veyra/api/routes.py` — three endpoints

Copy the shape of an existing trio (e.g. `/api/pvp/start`, `/api/pvp/stop`, `/api/pvp/status`):

```
POST /api/dungeon-pvp/start
POST /api/dungeon-pvp/stop
GET  /api/dungeon-pvp/status
```

Each requires a connected account (use the same auth/session helper the other routes use).

#### 7. Frontend — `veyra/web/index.html`, `veyra/web/app.js`, `veyra/web/style.css`

The current dashboard is a single page with collapsible sections per worker. Add one new section labeled **"Guild Dungeon (PvP)"** in the same style. It should render:

- A `Start` / `Stop` button (toggles based on `running`).
- A status line: `Last action: …`, `Next wake: …`, `Cooldowns: ring_ward 01:56:31 · duel_heart —`.
- The standard last-error line.

No new tabs are needed. Just another section that fits the existing layout. Match the surrounding CSS — do not introduce a new design language.

---

## Code conventions (read CLAUDE.md too)

- **Security (from `CLAUDE.md`):** never commit `.env`/`veyra.db`/`profiles.json`; never log passwords; never weaken DocsGuard. Credentials flow through `veyra/config.py` (`Settings` with `env_prefix="VEYRA_"`). Don't hardcode anything.
- **No over-engineering.** Don't add abstractions beyond what's needed. Don't add error handling for cases that can't happen. Don't add backwards-compat shims. The existing workers are pragmatic and concise — match that energy.
- **No unnecessary comments.** Only add a comment when the *why* is non-obvious (a hidden constraint, a workaround). Don't narrate the *what*.
- **No new top-level files.** All code goes inside `veyra/`. The plan doc and this prompt are the only Markdown files at the root.
- **No new dependencies.** Use what's already in `pyproject.toml` (httpx, BeautifulSoup, SQLAlchemy, FastAPI). The parsing here is plain regex + bs4 — no new libs needed.
- **Python 3.12.** Use `match` statements where they fit, `dataclasses`, modern type hints (`int | None`).

---

## How to test (the human will run these)

1. `source .venv/bin/activate && python -m veyra.main` (or `./START.sh`)
2. Open `http://127.0.0.1:5678/`, connect the dev account.
3. The new "Guild Dungeon (PvP)" section should appear; press **Start**.
4. Watch the SSE log for `[dungeon-pvp] …` lines. Expected first cycle:
   - Resolves instance_id from guild_dash.
   - Skips ring_ward (cooldown active from a prior probe — ~2h) OR joins it (if the test runs after the cooldown elapses).
   - Skips duel_heart (status=hidden until ring_ward fully clears).
   - Sleeps until cooldown_expiry+60s if the cooldown is < 30 min, else 30 min.
5. Press **Stop** — the worker should exit promptly.
6. Confirm `GET /api/dungeon-pvp/status` returns the expected shape.

---

## Out of scope for this PR

Don't implement these — they're in the plan but separate workstreams:

- **Army-room worker** (Veil Post / Captain Spine / Abyssal Muster) — separate file, separate ticket. The plan has the contracts (`guild_dungeon_cube_army_action.php` with `action=state | contributors | enter_fight`) but we want the PvP one shipped first.
- **Cross-instance cooldown reset semantics.** Just do the per-cycle check; we'll harden it when the dungeon expires.
- **In-fight skill use.** The match auto-resolves once 5 are in; we don't drive turns.
- **Persistence of `last_join_at`.** Keep state in-memory on the singleton for now; surviving restarts is a separate ticket.

---

## Definition of done

- All three endpoints reachable; Start/Stop work from the UI.
- Worker successfully completes one full cycle (probe → either join or note cooldown → sleep) without errors against the live game.
- A second cycle correctly observes the cooldown the first one created and skips the affected room.
- Code passes whatever tests exist in `tests/` (likely none for new workers — that's fine).
- No changes to `.gitignore`, `CLAUDE.md`, `PLAN.md`, or `GUILD_DUNGEON_PLAN.md`.

If anything in [`GUILD_DUNGEON_PLAN.md`](GUILD_DUNGEON_PLAN.md) conflicts with this prompt, the plan wins — flag the conflict, don't silently resolve it.
