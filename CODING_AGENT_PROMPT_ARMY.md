# Coding Agent Prompt — Guild Dungeon Army Worker

Hand this entire file to a fresh coding agent. It assumes access to the repo at `/Users/goreanuvictor/Desktop/scansGame`. The PvP guild-dungeon worker is **already implemented and shipped** ([`veyra/engine/dungeon_pvp_farmer.py`](veyra/engine/dungeon_pvp_farmer.py)) — use it as a structural template and follow the same conventions.

---

## Goal

Add a second 24/7 background worker for the **Army-side** rooms inside *The Polyhedral Crucible* — sibling to the existing PvP worker. Surface it as a separate panel in the Guild section of the dashboard with its own Start/Stop button.

The full design — endpoints, picker, retreat semantics — is already written up in **[`GUILD_DUNGEON_PLAN.md`](GUILD_DUNGEON_PLAN.md) Section 5**. **Read Section 5 first and treat it as the spec.** This prompt is a build checklist on top of it.

---

## What the user wants

Verbatim from the user:

> "for these we will want to join in battles… we just need to hit as little as possible over 0 and then we can leave the fight and still get the rewards"

> "we will start with Shadow Encirclement, Veil Post, and then after all monsters are beaten the other post opens and then the boss. For a post there are 4 fights and we need to start with first one cleared, Fight #1 → after that is dealt with we need to join Fight #2, etc. We can check every 30 min for available fights. Once we joined a fight we can't join the same one or other ones, so we wait for that one to die. Same for boss."

Translation:
- Tap-and-run on every fight: join → assign one target → retreat all our captains immediately → leave with ≥1 hit landed and ≤ 1 swing's damage on the books.
- One fight at a time across the entire run (server enforces this).
- 30-min poll cadence is fine — no adaptive scheduler is needed for army (unlike PvP, there's no per-player cooldown to chase).
- Sequence: `Veil Post → Captain Spine → Abyssal Muster`.

---

## What's already verified

All endpoints have been hit against the live game. The exact sequence below succeeded on the dev account against `instance_id=7135 / Abyssal Muster (node 11) / match 1` — landing 61,643 damage on Fenraxx because we waited 15 s before retreating (the production worker should NOT wait). See Section 5.7 of the plan.

```
1. POST /guild_dungeon_cube_action.php
     action=enter_node, instance_id, node_id, face_key
   → { ok, redirect, state }

2. POST /guild_dungeon_cube_army_action.php
     action=state, instance_id, node_id
   → { ok, cards[{match_no, status, banner_name, participant_count, battle_id, ...}],
        active_match_no, required_matches, cleared_matches }

3. POST /guild_dungeon_cube_army_action.php
     action=enter_fight, instance_id, node_id, match_no
   → { ok, redirect: "shadow_army_live_battle.php?battle_id=N", battle_id, match_no }

4. POST /shadow_army_live_battle.php
     action=join_battle, battle_id
   → { ok, state }
   state.viewer.{has_roster, can_join, other_active_match_no, enemy_units_killed}
   state.captains[] — entries with is_mine=true are ours

5. POST /shadow_army_live_battle.php
     action=assign_target,
            battle_id,
            attacker_captain_unit_id,
            defender_captain_unit_id
   → { ok: true, message: "Target assigned.", state }

6. POST /shadow_army_live_battle.php
     action=retreat_captain, battle_id, captain_unit_id
   → { ok: true, message: "Retreat queued. It will happen after the next enemy hit." }
   Retreat is queued — our captain still attacks once before leaving. This is the
   "minimum damage" guarantee: do NOT sleep between assign_target and retreat.
```

Reference probe scripts (read-only): `/tmp/probe_army.py`, `/tmp/probe_abyssal_live.py`. Saved page samples: `/tmp/abyssal_muster_enter_live.html`, `/tmp/abyssal_muster_battle.html`, `/tmp/abyssal_muster_state_live.json`, `/tmp/abyssal_join_response.json`. Do not import from these — they're inspection artifacts, not production code.

---

## What to build

### 1. `veyra/game/endpoints.py` — add URLs

```python
GUILD_DUNGEON_CUBE_ARMY_ACTION_URL = f"{BASE_URL}/guild_dungeon_cube_army_action.php"
SHADOW_ARMY_LIVE_BATTLE_URL        = f"{BASE_URL}/shadow_army_live_battle.php"

DUNGEON_ARMY_TARGET_KEYS = ("veil_post", "captain_spine", "abyssal_muster")
```

The PvP worker already added `GUILD_DUNGEON_CUBE_URL` and `GUILD_DUNGEON_CUBE_ACTION_URL` — reuse those.

### 2. `veyra/game/types.py` — add a card dataclass

```python
@dataclass
class ArmyMatchCard:
    match_no: int
    status: str            # 'open' | 'active' | 'cleared' (verify exact set; 'cleared' is confirmed)
    winner_side: str       # '' while live; 'ALLY' / 'ENEMY' once resolved
    banner_name: str
    participant_count: int
    battle_id: int
    run_id: int | None = None
    total_damage: int | None = None
    total_kills: int | None = None
```

### 3. `veyra/game/parser.py` — none needed

The army action endpoints return JSON directly. Parse with `httpx.Response.json()` and the dataclass above. No HTML parsing required for the army flow (unlike PvP, which had to scrape the node landing page).

### 4. `veyra/game/client.py` — add HTTP wrappers

Match the existing referer / `X-Requested-With: fetch` / form-urlencoded conventions (see how `pvp_pick_slot` / `enter_cube_node` are written). All four wrappers are POSTs to JSON-returning endpoints; just propagate the `ok / message / state` structure.

```python
async def fetch_army_node_state(self, instance_id: str, node_id: int|str) -> dict:
    """POST guild_dungeon_cube_army_action.php action=state. Returns the full payload."""

async def army_enter_fight(self, instance_id: str, node_id: int|str, match_no: int) -> dict:
    """POST action=enter_fight. Returns {ok, redirect, battle_id, match_no}."""

async def shadow_battle_state(self, battle_id: int) -> dict:
    """GET shadow_army_live_battle.php?battle_id=X and parse the inline `const initialState`.
    Used to check `viewer.other_active_match_no` / `is_mine` captains BEFORE calling join_battle."""

async def shadow_battle_join(self, battle_id: int) -> dict:
    """POST shadow_army_live_battle.php action=join_battle."""

async def shadow_battle_assign_target(
    self, battle_id: int, attacker_captain_unit_id: int, defender_captain_unit_id: int
) -> dict: ...

async def shadow_battle_retreat(self, battle_id: int, captain_unit_id: int) -> dict: ...
```

For `shadow_battle_state`: since the page bootstraps `const initialState = {...};` inline as JSON, the easiest path is `regex extract → json.loads`. Add a tiny private parser helper near the others rather than polluting `parser.py` with HTML scrapes.

### 5. `veyra/engine/dungeon_army_farmer.py` — NEW file

Mirror the structure of [`dungeon_pvp_farmer.py`](veyra/engine/dungeon_pvp_farmer.py). The status dataclass and worker entrypoint look almost identical:

```python
@dataclass
class DungeonArmyStatus:
    running: bool = False
    last_action_at: str | None = None
    last_action: str | None = None
    next_wake_at: str | None = None
    last_error: str | None = None
    progress: dict[str, str] = field(default_factory=dict)  # room_key -> human status
    logs: list[dict] = field(default_factory=list)
    _log_id: int = 0
    # log() and stop() identical to DungeonPvpStatus
```

Worker constants:
```python
DEFAULT_TICK_S = 30 * 60
SLEEP_TICK_S = 5
```

**Cycle logic (one cycle per 30 min):**

```
1. Resolve The Polyhedral Crucible instance_id from /guild_dash.php (use
   client.fetch_open_dungeons — already exists from PvP worker).
2. Fetch cube STATE; build nodes_by_key.
3. For key in ("veil_post", "captain_spine", "abyssal_muster"):
     a. node = nodes_by_key.get(key); skip if missing.
     b. If node.status == 'hidden' OR node.is_cleared == 1: log + skip.
     c. POST enter_cube_node(instance_id, node.id, node.face_key).
        Don't require the redirect; this just opens the node.
     d. army_state = fetch_army_node_state(instance_id, node.id)
        Pick the lowest match_no in cards[] with status != 'cleared'.
        If none, log "no open fight" + skip to next room.
     e. ef = army_enter_fight(instance_id, node.id, match_no)
        battle_id = ef['battle_id']
     f. battle_state = shadow_battle_state(battle_id)
        # Already-engaged guards:
        if battle_state.viewer.other_active_match_no != 0:
            log "already engaged elsewhere" + STOP THE CYCLE (return early; the
            server will reject any further joins).
        if any(c.is_mine for c in battle_state.captains):
            log "already participating in this fight" + STOP THE CYCLE.
     g. join = shadow_battle_join(battle_id)
        if not join.ok:
            log error + STOP THE CYCLE.
     h. Find one of our captains (is_mine, not dead, not retreat_requested).
        Find one alive enemy captain.
        If either is missing, log + try retreat-all + STOP THE CYCLE.
     i. shadow_battle_assign_target(battle_id, my.id, enemy.id)
     j. NO SLEEP. For every is_mine captain still alive and not yet retreating:
          shadow_battle_retreat(battle_id, captain.id)
        Log: "joined Veil Post fight #1 (battle 9559), retreated after 1 swing."
        Update status.last_action / last_action_at / progress[key].
     k. STOP THE CYCLE — we've used our one allowed join for this poll.
4. Compute next wake = now + 1800s. Sleep with stop checks every 5s.
```

**Notes on the cycle:**
- *Stop the cycle* means break out of the for-loop and proceed to sleep. We do NOT continue trying other rooms on the same tick once we've successfully joined any fight; the server enforces a one-active-match limit per player.
- Site-down handling: same pattern as the PvP worker — `if game.is_site_down: await game.wait_for_site_up(...)` at the top of each cycle.
- **Defensive retreats on errors**: if we successfully called `join_battle` but failed `assign_target` or any other later step, fall through to the retreat-all loop (step j). We must not leave captains stuck in a fight without retreat requested.

### 6. `veyra/engine/account_manager.py` — wire it in

Mirror the PvP wiring exactly. Add `dungeon_army_status: DungeonArmyStatus`, `dungeon_army_task: asyncio.Task | None`, plus `is_dungeon_army_running` / `start_dungeon_army` / `stop_dungeon_army` / `get_dungeon_army_state` / `get_dungeon_army_status`. Cancel the task on disconnect.

### 7. `veyra/api/routes.py` — three endpoints + plumbing

Copy the PvP block:
```
POST /api/dungeon-army/start
POST /api/dungeon-army/stop
GET  /api/dungeon-army/status
```
Extend `/api/session` and `/api/status` with `dungeon_army_running` + `dungeon_army_status`. Add the SSE log relay block (see how PvP does it — `dpvp_last_id`).

### 8. Frontend — three files

Add a sibling panel **right next to** the existing "Guild Dungeon (PvP)" panel in [`veyra/web/index.html`](veyra/web/index.html):

```html
<span class="panel-title">
  Guild Dungeon (Army)
  <span class="pvp-badge off" id="dungeonArmyBadge">OFF</span>
</span>
…
<div class="dpvp-info">
  <span class="quest-info" id="dungeonArmyAction">Idle</span>
  <span class="quest-info" id="dungeonArmyNextWake"></span>
  <span class="quest-info" id="dungeonArmyProgress"></span>
  <span class="quest-info dpvp-error" id="dungeonArmyError"></span>
</div>
<div class="quest-controls">
  <button class="btn btn-sm btn-pvp" id="dungeonArmyStartBtn" onclick="startDungeonArmy()">Start</button>
  <button class="btn btn-sm" id="dungeonArmyStopBtn" onclick="stopDungeonArmy()" style="display:none">Stop</button>
</div>
```

Add `startDungeonArmy / stopDungeonArmy / updateDungeonArmyStatus` in [`veyra/web/app.js`](veyra/web/app.js) — copy the PvP versions, change endpoint URLs and DOM IDs. Wire `updateDungeonArmyStatus(data.dungeon_army_running, data.dungeon_army_status)` into the polling loop and session-restore branch the same places PvP is wired.

`progress` is a `dict[room_key, str]` — render as: `Veil Post: F#2 cleared · Captain Spine: hidden · Abyssal Muster: hidden`. Use the same separator pattern as the PvP `cooldowns` line.

No new CSS rules needed — reuse `.dpvp-info` / `.dpvp-error` / `.pvp-badge`.

---

## Conventions (read CLAUDE.md too)

Same rules as the PvP prompt:

- **Security:** never commit `.env` / `veyra.db` / `profiles.json`; never log passwords; settings flow through `veyra/config.py`.
- **No over-engineering, no premature abstraction, no comments narrating the *what*.** Match the prevailing style of `pvp_fighter.py` / `dungeon_pvp_farmer.py` — terse, dataclasses, async, plain logging.
- **No new top-level files.** Don't create README/markdown — only the code.
- **No new dependencies.** Use httpx + bs4 + stdlib only.
- **Python 3.12.** Modern syntax, `int | None`, `match` where it fits.
- **Don't touch** [`GUILD_DUNGEON_PLAN.md`](GUILD_DUNGEON_PLAN.md), [`CODING_AGENT_PROMPT.md`](CODING_AGENT_PROMPT.md), [`CLAUDE.md`](CLAUDE.md), [`PLAN.md`](PLAN.md). If reality conflicts with the plan, flag it — don't silently update it.

---

## How to test (the human will run these)

1. `python -m veyra.main`
2. Connect dev account via the dashboard.
3. New panel **"Guild Dungeon (Army)"** appears under the Guild section. Press **Start**.
4. Watch SSE logs for `[dungeon-army] …` lines. Expected on a current dungeon (instance 7135 at time of writing):
   - Veil Post → `is_cleared=1` → skip
   - Captain Spine → `is_cleared=1` → skip
   - Abyssal Muster → finds match #1 active → enter_fight → battle_state shows we already participated (we have a captain in there from the earlier probe with `retreat_requested=true`). Worker should **detect "already participating"** via the `is_mine` captain check and stop the cycle gracefully. Log line: `[dungeon-army] abyssal_muster: already participating in match #1, waiting`.
5. Wait for the boss to die / next dungeon instance to open, run again. Worker should now find a fresh fight, join, deal ≪ 5k damage, retreat all captains, log success, sleep 30 min.
6. **Stop** button — task cancels promptly within the 5-s sleep tick.
7. Confirm `GET /api/dungeon-army/status` returns the expected shape including `progress` with one entry per room we touched.

---

## Out of scope

Don't implement these — separate workstreams:

- Aggressive (full-clear) army participation. We're a tap-and-run leech, not a damage dealer.
- Picking specific captains/enemies for stat-optimal damage. Any captain × any enemy is fine; the swing > 0 is all that matters.
- Reward inventory tracking. The Shadow EXP Scrolls land in inventory automatically when each match resolves; we don't read them.
- Cross-instance state. Cooldowns / locks reset when a new dungeon instance opens; the worker's per-cycle re-discovery handles this transparently.

---

## Definition of done

- All three `/api/dungeon-army/*` endpoints respond correctly. Start/Stop usable from the UI.
- Worker runs a clean cycle that respects "one active match per player" — verified by the existing engagement on Abyssal Muster (worker should detect and gracefully wait, not retry).
- When the next opportunity opens, worker successfully joins → assigns target → retreats every captain in a single tick with zero in-between sleeps.
- Total damage dealt per fight is roughly one captain swing (≪ 5,000), not five-figure values.
- No regression in the PvP worker — both can run simultaneously on the same connected account.
- Clean import (`python -m veyra.main` boots without errors).
