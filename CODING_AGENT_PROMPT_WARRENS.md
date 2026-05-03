# Coding Agent Prompt — Gribble Farmer (Shadowbridge Warrens)

Hand this entire file to a fresh coding agent. It assumes access to the repo at `/Users/goreanuvictor/Desktop/scansGame`. The PvP and Army guild-dungeon workers are **already implemented and shipped** — use them as templates.

- Reference: [`veyra/engine/dungeon_pvp_farmer.py`](veyra/engine/dungeon_pvp_farmer.py)
- Reference: [`veyra/engine/dungeon_army_farmer.py`](veyra/engine/dungeon_army_farmer.py)

Reuse the exact same dataclass shape, log/stop conventions, account-manager wiring pattern, route trio, SSE relay block, and frontend panel structure.

---

## Goal

Add a third 24/7 background worker that farms **Gribble Junk-Magus** monsters inside the **Shadowbridge Warrens** dungeon — strictly to clear the personal damage threshold (1,000,000) on each one for the **Full Stamina Potion** drop, then move on. Surface it in the Guild section of the dashboard with its own Start/Stop button and status line.

This is **not** a generic Warrens farmer. It only targets one mob name. Do not generalize — the user wants a focused, predictable leech worker.

---

## What the user wants (verbatim)

> "what i am looking for is joining the battle and doing 1 mil damage to get the Full Stamina Potion (Drop: 50%, DMG req: 1,000,000). I need to do just above 1 mil and stop — so 1,000,000 is fine, 1,000,001 is fine, 999,999 is not enough damage. We do 10 stamina per hit, should take 2 hits. We can check every 3 hours for available Gribbles. We don't use potions to refill stamina — if we don't have enough we just leave it for the next one to finish the required damage. If the monster already has 1 mil from us we don't need to do more, so it should never do that. After every hit you check that monster damage."

Translation:
- Per Gribble: leech to ≥ 1,000,000 personal damage and stop.
- Skip cleanly if our damage on a Gribble is already ≥ 1M.
- Skip cleanly if our stamina < 10 (the per-hit cost).
- **Never** call any "use potion" flow.
- Re-check damage after **every** hit using `totaldmgdealt` from the `damage.php` response.
- 3-hour cadence, no adaptive scheduling needed.

---

## What's already verified

**The full flow has been probed live and an end-to-end test passed.** See [`/tmp/test_gribble_farm.py`](file:///tmp/test_gribble_farm.py) for the working reference (do not import from it; it's a probe script, not production code).

Test result on the dev account against `instance_id=7133`, Gribble `dgmid=1349602`:
- Pre-state: `my_damage=None` (not on leaderboard), `join_status='not joined'`, HP `180M/200M`, stamina `70`
- Join → `"You have successfully joined this battle."`
- Hit #1: `totaldmgdealt=535,898`
- Hit #2: `totaldmgdealt=1,071,796` ← crossed threshold, stopped
- Re-run picker on same Gribble → `SKIP — my damage 1,071,796 ≥ 1,000,000, no attack needed`

### Endpoint contracts (verified)

```
1. GET /guild_dungeon_location.php?instance_id={iid}&location_id={N}
   Where N ∈ {2 (Plunder Warrens), 4 (Territory Center)} — the only rooms that
   contain Gribble Junk-Magus in this dungeon.
   The page renders one <div class="mon"> card per monster; cards with class
   modifier 'dead' are corpses. Each card has a Fight link
   <a href="battle.php?dgmid={N}&instance_id={iid}"> from which we read the dgmid.

2. GET /battle.php?dgmid={N}&instance_id={iid}
   Per-monster page. Two things to scrape:
     - Leaderboard: <div class="lb-row"> entries.
       My row is the one containing <a href="player.php?pid={USER_ID}">.
       Damage is the sibling <span class="lb-dmg">N,NNN DMG</span>.
       If my row is absent → my damage is 0 (haven't joined / not contributed).
     - Stamina: <span id="stamina_span">2,170</span>
     - Join status:
         "haven't joined" / "haven’t joined" text → not joined
         <button id="join-battle"> present → not joined
         otherwise → joined
     - Monster HP: HP <strong>N,NNN</strong> / N,NNN — if current HP is 0,
       the monster is dead.

3. POST /dungeon_join_battle.php
   Body: instance_id, dgmid, user_id
   (user_id comes from GameClient.user_id which is already populated by the
   existing flow; if not, fall back to scraping pid=… from the page.)
   Response is plain text. Success starts with "You have successfully".

4. POST /damage.php
   Body: instance_id, dgmid, skill_id=-1, stamina_cost=10
   Response (JSON): contains `totaldmgdealt` (cumulative MY damage on this
   monster, server-tracked), `monsterdead` (or `monster_dead`), and the same
   leaderboard array used by the page.
   `totaldmgdealt` is the authoritative per-hit progress signal.
```

### Damage profile

On the dev account (LV 894), one 10-stamina hit on a Gribble deals ~535k damage. So **2 hits** crosses the 1M threshold (matches the user's prediction). A weaker account would need 3+ hits — the per-hit re-check handles this transparently.

---

## What to build

### 1. `veyra/game/endpoints.py` — add URLs

```python
GUILD_DUNGEON_INSTANCE_URL  = f"{BASE_URL}/guild_dungeon_instance.php"
GUILD_DUNGEON_LOCATION_URL  = f"{BASE_URL}/guild_dungeon_location.php"
DUNGEON_JOIN_BATTLE_URL     = f"{BASE_URL}/dungeon_join_battle.php"
SHADOWBRIDGE_WARRENS_NAME   = "Shadowbridge Warrens"
WARRENS_GRIBBLE_LOCATIONS   = (2, 4)         # Plunder Warrens, Territory Center
WARRENS_GRIBBLE_NAME        = "Gribble Junk-Magus"
WARRENS_DAMAGE_THRESHOLD    = 1_000_000
WARRENS_STAMINA_PER_HIT     = 10
WARRENS_STAMINA_SKILL_ID    = "-1"
```

`DAMAGE_URL` already exists (used by the wave farmer); reuse it for the dungeon attack — only the body differs (uses `dgmid` + `instance_id` instead of `monster_id`).

### 2. `veyra/game/types.py` — add a card dataclass

```python
@dataclass
class WarrensMonsterCard:
    dgmid: str
    name: str
    is_dead: bool
    hp_current: int | None
    hp_max: int | None
```

### 3. `veyra/game/parser.py` — three parsers

```python
def parse_warrens_monsters(html: str) -> list[WarrensMonsterCard]:
    """Parse all <div class='mon'> cards on a guild_dungeon_location.php page.
    Each card contains a Fight link with dgmid in the href and the monster name
    in the card text. 'dead' modifier on the class indicates a corpse."""

def parse_my_dungeon_damage(html: str, user_id: str) -> int:
    """Scan div.lb-row for a child <a href='player.php?pid={user_id}'>.
    Return the integer in the sibling .lb-dmg span (commas stripped).
    Return 0 if our row is absent."""

def parse_dungeon_battle_status(html: str) -> dict:
    """Returns {
       joined: bool,                 # True if we have already joined this battle
       stamina: int | None,          # current stamina from <span id="stamina_span">
       monster_alive: bool,
       hp_current: int, hp_max: int,
    }"""
```

Helpers/regexes that work (verified live):
- Stamina: `r'id\s*=\s*["\']stamina_span["\'][^>]*>\s*([\d,]+)'`
- Join status (not joined): `r"haven[’']t\s+joined"` OR presence of `<button[^>]+id\s*=\s*["\']join-battle["\']`
- HP: `r"HP\s*<strong>([\d,]+)</strong>\s*/\s*([\d,]+)"`

### 4. `veyra/game/client.py` — add wrappers

Match the existing referer/header conventions (see how `pvp_pick_slot` / `enter_cube_node` / `attack` are written):

```python
async def fetch_warrens_room(self, instance_id: str, location_id: int) -> tuple[str, list[WarrensMonsterCard]]:
    """GET guild_dungeon_location.php and return (raw_html, parsed_cards)."""

async def fetch_dungeon_battle_page(self, instance_id: str, dgmid: str) -> str:
    """GET battle.php?dgmid=…&instance_id=… — used by the worker for both
    leaderboard read and join-status check."""

async def join_dungeon_battle(self, instance_id: str, dgmid: str) -> tuple[bool, str]:
    """POST dungeon_join_battle.php. Returns (ok, server_message).
    Success message starts with 'You have successfully'."""

async def attack_dungeon_monster(
    self, instance_id: str, dgmid: str, stamina_cost: int = 10, skill_id: str = "-1"
) -> dict:
    """POST damage.php with dgmid form. Returns the parsed JSON; the worker
    only reads `totaldmgdealt` and `monsterdead` (or `monster_dead`)."""
```

### 5. `veyra/engine/dungeon_warrens_farmer.py` — NEW file

Mirror [`dungeon_army_farmer.py`](veyra/engine/dungeon_army_farmer.py) shape exactly. Key constants:

```python
DEFAULT_TICK_S = 3 * 60 * 60     # 3 hours, per the user's spec
SLEEP_TICK_S = 5
HIT_CAP_PER_GRIBBLE = 10         # safety bound — should never need more than ~3 hits
```

Dataclass:
```python
@dataclass
class DungeonWarrensStatus:
    running: bool = False
    last_action_at: str | None = None
    last_action: str | None = None
    next_wake_at: str | None = None
    last_error: str | None = None
    progress: dict[str, str] = field(default_factory=dict)  # e.g. {"loc 2": "5/10 farmed"}
    totals: dict[str, int] = field(default_factory=dict)    # cumulative across cycles
    logs: list[dict] = field(default_factory=list)
    _log_id: int = 0
    # log() and stop() identical to the others
```

**Per cycle (every 3h):**

```
1. fetch_open_dungeons() — find SHADOWBRIDGE_WARRENS_NAME → instance_id.
   If no instance: log + sleep 3h.

2. for location_id in WARRENS_GRIBBLE_LOCATIONS:    # (2, 4)
     a. fetch_warrens_room(iid, location_id) → cards
     b. gribbles = [c for c in cards
                    if c.name == WARRENS_GRIBBLE_NAME and not c.is_dead]
     c. for gribble in gribbles:
          - if not status.running: break
          - farm_one(gribble) — see below

3. Sleep DEFAULT_TICK_S (3h) with stop checks.
```

**`farm_one(gribble)` — the core picker (mirrors `/tmp/test_gribble_farm.py`):**

```
A. fetch_dungeon_battle_page(iid, gribble.dgmid)
B. parse_my_dungeon_damage(html, USER_ID) → my_damage
C. parse_dungeon_battle_status(html) → {joined, stamina, monster_alive, hp_*}
D. SKIP RULES (in order):
     - if my_damage >= WARRENS_DAMAGE_THRESHOLD → log "skip: ≥1M" + return
     - if not monster_alive → log "skip: dead" + return
     - if stamina is None or stamina < WARRENS_STAMINA_PER_HIT
       → log "skip: stamina N < 10, leaving for next cycle" + return
E. If not joined:
     ok, msg = await join_dungeon_battle(iid, dgmid)
     if not ok: log + return
F. cumulative = my_damage      # baseline
   for hit in range(HIT_CAP_PER_GRIBBLE):
       if cumulative >= WARRENS_DAMAGE_THRESHOLD: break
       if stamina < WARRENS_STAMINA_PER_HIT: break  # never use potions
       resp = await attack_dungeon_monster(iid, dgmid)
       cumulative = int(resp.get("totaldmgdealt", cumulative))
       stamina = int(resp.get("stamina_left",
                              resp.get("stamina",
                                       (stamina or 0) - WARRENS_STAMINA_PER_HIT)))
       if resp.get("monsterdead") or resp.get("monster_dead"):
           break
G. Update status.progress[f"loc {location_id}"] and status.totals.
   Log: "Gribble dgmid=N: my_damage=K (after H hits), stamina=S".
H. Return.
```

**Important guards**:
- The hit-loop must re-check `cumulative >= 1_000_000` **before** every attack and **immediately** after each response — not just at the loop bottom. See the test script for the exact pattern.
- Never call any potion endpoint. The worker must not have a code path that consumes potions.
- If `parse_my_dungeon_damage` returns 0 and `parse_dungeon_battle_status.joined == True`, that's normal (we joined but haven't hit yet) — proceed to the hit loop with `cumulative = 0`.

**Logging format** (match the existing two workers' style):
```
=== Guild Dungeon Warrens started ===
[dungeon-warrens] cycle start
[dungeon-warrens] resolved Shadowbridge Warrens instance_id=7133
[dungeon-warrens] loc 2 (Plunder Warrens): 10 alive Gribbles
[dungeon-warrens] dgmid=1349602: pre my_damage=0, stamina=70, joined=no
[dungeon-warrens] dgmid=1349602: joined ('You have successfully joined this battle.')
[dungeon-warrens] dgmid=1349602: hit #1 totaldmgdealt=535,898 stamina_left=60
[dungeon-warrens] dgmid=1349602: hit #2 totaldmgdealt=1,071,796 stamina_left=50 — DONE
[dungeon-warrens] dgmid=1349603: pre my_damage=0, stamina=50, joined=no
…
[dungeon-warrens] loc 2: 5/10 farmed this cycle (4 already ≥1M, 1 skipped: stamina)
[dungeon-warrens] sleeping 10800s (until …)
```

### 6. `veyra/engine/account_manager.py` — wire it in

Mirror the army-worker wiring exactly. Add `dungeon_warrens_status: DungeonWarrensStatus`, `dungeon_warrens_task: asyncio.Task | None`, plus `is_dungeon_warrens_running` / `start_dungeon_warrens` / `stop_dungeon_warrens` / `get_dungeon_warrens_state` / `get_dungeon_warrens_status`. Cancel the task on disconnect.

### 7. `veyra/api/routes.py` — three endpoints + plumbing

Copy the army block:
```
POST /api/dungeon-warrens/start
POST /api/dungeon-warrens/stop
GET  /api/dungeon-warrens/status
```
Extend `/api/session` and `/api/status` with `dungeon_warrens_running` + `dungeon_warrens_status`. Add the SSE log relay block (`dwarrens_last_id`).

### 8. Frontend — three files

Add a sibling panel next to the Army panel in [`veyra/web/index.html`](veyra/web/index.html):

```html
<span class="panel-title">
  Guild Dungeon (Warrens — Gribble)
  <span class="pvp-badge off" id="dungeonWarrensBadge">OFF</span>
</span>
…
<div class="dpvp-info">
  <span class="quest-info" id="dungeonWarrensAction">Idle</span>
  <span class="quest-info" id="dungeonWarrensNextWake"></span>
  <span class="quest-info" id="dungeonWarrensProgress"></span>
  <span class="quest-info dpvp-error" id="dungeonWarrensError"></span>
</div>
<div class="quest-controls">
  <button class="btn btn-sm btn-pvp" id="dungeonWarrensStartBtn" onclick="startDungeonWarrens()">Start</button>
  <button class="btn btn-sm" id="dungeonWarrensStopBtn" onclick="stopDungeonWarrens()" style="display:none">Stop</button>
</div>
```

Add `startDungeonWarrens / stopDungeonWarrens / updateDungeonWarrensStatus` in [`veyra/web/app.js`](veyra/web/app.js) — copy the army versions, change endpoint URLs and DOM IDs. Wire `updateDungeonWarrensStatus(data.dungeon_warrens_running, data.dungeon_warrens_status)` into the polling loop, session-restore branch, and `DOMContentLoaded` badge init.

`progress` renders as `loc 2: 5/10 farmed · loc 4: 3/5 farmed`. No new CSS rules — reuse `.dpvp-info` / `.dpvp-error` / `.pvp-badge`.

---

## Conventions

Same rules as the previous two prompts:

- **Security:** never commit `.env` / `veyra.db` / `profiles.json`; never log passwords; never weaken DocsGuard. Settings flow through `veyra/config.py`.
- **No over-engineering, no premature abstraction, no narrative comments.** Match the prevailing terse style of the existing workers.
- **No new top-level files.** Don't create README/markdown — only code.
- **No new dependencies.** httpx + bs4 + stdlib only.
- **Python 3.12.** Modern syntax, `int | None`, `match` where it fits.
- **Don't touch** [`GUILD_DUNGEON_PLAN.md`](GUILD_DUNGEON_PLAN.md), [`CODING_AGENT_PROMPT.md`](CODING_AGENT_PROMPT.md), [`CODING_AGENT_PROMPT_ARMY.md`](CODING_AGENT_PROMPT_ARMY.md), [`CLAUDE.md`](CLAUDE.md), [`PLAN.md`](PLAN.md). If reality conflicts with the plan, flag it — don't silently update it.

---

## Out of scope

- Other Warrens mobs. We only target `Gribble Junk-Magus`.
- Fighting in `brood pits` (loc 1) or `Shattered Stone Causeways` (loc 3). No Gribbles there.
- Boss room. Locked, not relevant to this drop.
- Stamina potion auto-refill. Explicitly forbidden by the user — if stamina < 10 we leave the Gribble for the next cycle.
- Persisting per-Gribble damage state across restarts. The leaderboard is the source of truth; we re-read it every cycle.
- Cross-instance carryover. When a new Shadowbridge Warrens instance opens, all Gribbles are fresh — the worker handles this transparently because it re-discovers `instance_id` and `dgmid`s every cycle.

---

## How to test (the human will run these)

1. `python -m veyra.main`
2. Connect dev account.
3. New panel **"Guild Dungeon (Warrens — Gribble)"** appears in the Adventure Guild tab. Press **Start**.
4. Watch SSE logs for `[dungeon-warrens] …` lines. Expected first cycle (against the current instance, where dgmid 1349602 already has us at ~1.07M damage from the test):
   - `loc 2 (Plunder Warrens): 10 alive Gribbles`
   - `dgmid=1349602: pre my_damage=1,071,796 ≥ 1,000,000, skipping`
   - `dgmid=1349603: pre my_damage=0, joining + hitting…`
   - For each Gribble we farm, ~2 hits to cross 1M, ~20 stamina per Gribble.
   - Eventually: `dgmid=…: stamina 0 < 10, leaving for next cycle` (when we run out — happens at ~7-9 fresh Gribbles depending on starting stamina).
   - `loc 4 (Territory Center): 5 alive Gribbles` if we still have stamina, otherwise the same pre-hit skip will fire.
   - Final: `sleeping 10800s (until …)`.
5. Wait 3 hours OR press **Stop** and **Start** again. Expected:
   - All Gribbles we already cleared show `pre my_damage ≥ 1M, skipping` — **NO attack calls**.
   - This is the test the user explicitly asked for.
6. Check `GET /api/dungeon-warrens/status`:
   - `running: true`, `last_action_at` set, `progress` populated, `totals` cumulative across cycles.
7. Stop responsiveness: **Stop** button cancels the worker within ~5s (the sleep tick).

---

## Definition of done

- All three `/api/dungeon-warrens/*` endpoints respond correctly. Start/Stop work from the UI.
- A clean cycle:
  - Discovers the current Shadowbridge Warrens instance.
  - Iterates rooms 2 and 4.
  - For each Gribble: applies the three skip rules in order, then if eligible, joins (if needed) and hits in a loop with per-hit re-check, stopping the moment `totaldmgdealt ≥ 1,000,000`.
  - Total damage on each Gribble lands between 1,000,000 and ~1,500,000 (one swing past the line, never 5x past it).
- Re-running a cycle on the same instance correctly re-skips every previously-farmed Gribble using only the leaderboard scrape — no attacks fired on already-cleared targets.
- Stamina conservation: when the page reports stamina < 10, the worker advances to the next Gribble without firing `damage.php`.
- Coexists cleanly with the PvP and Army workers — three workers concurrent on one connected account.
- Boots without errors (`python -m veyra.main`).
