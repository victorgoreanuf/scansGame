# Guild Dungeon — Open Dungeons Plan

Plan for automating "Open Dungeons" rooms inside the **Guild** tab (separate from Adventure Guild). Captured from a live probe on 2026-05-03 against `The Polyhedral Crucible` instance `7081` (every match on every targeted room was already `cleared`, so the picker logic is not yet validated against a live match).

---

## 1. Scope

Two room types we want to automate inside `The Polyhedral Crucible` (a "cube" dungeon, `dungeon_id=3`):

### PvP-style rooms — Arena Bastion face
Up to 5 guild members per match. Joining a match grants a Large Stamina Potion every ~2 hours (per player).

| Room | `key` | `node_id` | `face_key` | Encounter | `unlock_rule` |
|---|---|---|---|---|---|
| Ring Ward | `ring_ward` | 7 | `right` | `pvp_encounter_id=1` | (none — always available) |
| Duel Heart | `duel_heart` | 8 | `right` | `pvp_encounter_id=7` | (none — always available) |

### Army rooms — Shadow Encirclement face
Live captain warfare. Joining grants books / EXP that interact well with the Shadow Army.

| Room | `key` | `node_id` | `face_key` | Encounter | `unlock_rule` |
|---|---|---|---|---|---|
| Veil Post | `veil_post` | 5 | `back` | `army_encounter_id=1` | (none — always available) |
| Captain Spine | `captain_spine` | 6 | `back` | `army_encounter_id=2` | (none — always available) |

### Subjugation Undercroft (chained / boss tier) — `bottom` face
Two boss rooms that only unlock once the corresponding non-boss rooms on the same path are fully cleared. Same endpoint contracts as the rooms above — the only thing different is the gating.

| Room | `key` | `node_id` | `face_key` | Encounter | `unlock_rule` | Description |
|---|---|---|---|---|---|---|
| Tyrant Conclave | `tyrant_conclave` | 9 | `bottom` | `pvp_encounter_id=13` | `all_pvp_other` | PvP-boss — opens after **every other PvP-style match in the Crucible** has been won (Ring Ward + Duel Heart fully cleared). |
| Abyssal Muster | `abyssal_muster` | 11 | `bottom` | `army_encounter_id=9` | `all_army_other` | Army-boss — opens after **every other army engagement in the Crucible** has fallen (Veil Post + Captain Spine fully cleared). |

The picker logic for each Undercroft room is identical to its non-boss siblings:
- `tyrant_conclave` is joined exactly like `ring_ward` / `duel_heart` (`POST pvp_style_action.php action=join_room`). Note `state_meta.pvp_rooms_total` is `1` (a single boss match), not 20.
- `abyssal_muster` is joined exactly like `veil_post` / `captain_spine` (army flow via `guild_dungeon_cube_army_action.php`).

**Worker scheduling:** before attempting an Undercroft room, re-fetch the cube state and confirm `node.status == 'available'` (or `is_revealed == 1` and not `is_cleared`) for the boss node. The worker that handles the non-boss rooms should re-trigger the cube fetch after a successful join, so it can pick up the boss room as soon as the unlock fires.

> ⚠️ `node_id` and `face_key` are tied to the **dungeon definition** (`DUNGEON_ID=3`), not the instance. They should be stable across instances of The Polyhedral Crucible, but the bot should still re-discover them by parsing each cube page's `STATE.nodes` and matching on `key` (e.g. `ring_ward`) — robust against the game shuffling IDs.

---

## 2. Page / endpoint flow

### 2.1 Discover open dungeon instances
```
GET https://demonicscans.org/guild_dash.php
```
Section `<h2 class="title">Open Dungeons</h2>` contains one card per open dungeon:
```html
<div ...card>
  <div style="font-weight:700">The Polyhedral Crucible</div>
  <div class="muted">Instance #7081 • Opened 2026-05-02 22:16:25</div>
  <a class="btn" href="guild_dungeon_enter.php?id=7081">Enter</a>
  <a class="btn" href="dungeon_info.php?id=3">Info</a>
</div>
```
Pull `instance_id` from the Enter link (`guild_dungeon_enter.php?id={iid}`). Match dungeon by name (`The Polyhedral Crucible`) so we don't confuse it with non-cube dungeons like `Shadowbridge Warrens` (dungeon_id=1, different page).

### 2.2 Land on the cube map
```
GET https://demonicscans.org/guild_dungeon_cube.php?instance_id={iid}
```
Page embeds two inline JSON blobs we need:
- `const FACE_DATA = {...}` — face → list of nodes (UI layout)
- `const STATE = {...}` — authoritative state with `nodes[]`, `selected_node`, `current_face_key`, instance metadata

Each node carries: `id`, `face_key`, `key`, `name`, `type` (`pvp` | `army` | `pve` | `boss` | `shop` | `forge` | `arrival`), `status`, and a `state_meta` block with progress counters.

For PvP nodes, `state_meta` looks like:
```json
{
  "pvp_rooms_total": 20,
  "pvp_rooms_spawned": 20,
  "pvp_rooms_cleared": 20,
  "pvp_rooms_live": 0,
  "pvp_rooms_open": 0,
  "winner": "ally",
  "turns": 12,
  "resolved_at": "2026-05-03 10:59:57"
}
```

For Army nodes:
```json
{
  "army_fights_total": 4,
  "army_fights_spawned": 4,
  "army_fights_cleared": 4,
  "army_fights_failed": 0,
  "army_fights_active": 0,
  "army_fights_open": 0,
  "last_opened_match_no": 4,
  "winner": "ALLY",
  "resolved_at": "2026-05-02 23:04:20"
}
```

### 2.3 Click "Enter" on a node
```
POST https://demonicscans.org/guild_dungeon_cube_action.php
  action=enter_node
  instance_id={iid}
  node_id={node.id}
  face_key={node.face_key}
```
Response:
```json
{ "ok": true, "redirect": "<url>", "state": {...} }
```

The redirect target depends on node type:

| Node type | Redirect URL pattern |
|---|---|
| PvP-style | `pvp_style_node.php?source=cube&instance_id={iid}&node_id={nid}` |
| Army | `guild_dungeon_cube_army_enter.php?instance_id={iid}&node_id={nid}` |

---

## 3. PvP-style room — match flow

### 3.1 Room landing page (efficient bulk read)
```
GET /pvp_style_node.php?source=cube&instance_id={iid}&node_id={nid}
```
One GET returns all `pvp_rooms_total` match cards in a single HTML page (20 for Ring Ward / Duel Heart, 1 for Tyrant Conclave). Each card is a `<div class="match">` whose plain text matches:
```
Match #<N> / Slots <occupants>/5  <STATUS_LABEL>
```
And contains an `<a href="pvp_style_battle.php?source=cube&instance_id={iid}&node_id={nid}&match_no={N}">`.

Observed status labels (and their `pvp_style_state.php` `room_status` equivalents):
| Label on node page | `room_status` | Meaning | Joinable? |
|---|---|---|---|
| `OPEN` / `Waiting` | `'open'` | Match exists, lobby is open | yes if `occupants < 5` |
| `LIVE` / `In battle` | `'live'` | 5 joined and the trial is running | no (always 5/5 anyway) |
| `CLEARED` | `'cleared'` | Resolved (`match.ended == True`) | no |

When all matches the node will ever host are not yet spawned (for example the moment after a fresh dungeon opens) the page still renders all `pvp_rooms_total` cards — it's not gated on spawn count.

> The bot's normal path is: **one GET to the node page**, parse all match cards, pick the candidate, then optionally **one GET to `pvp_style_state.php` for the chosen match** to verify `room_joined == False` before calling `join_room`. We avoid the 20× per-match state poll.

### 3.2 Per-match state poll
```
GET /pvp_style_state.php?source=cube&instance_id={iid}&node_id={nid}&match_no={N}
  Referer: pvp_style_battle.php?source=cube&instance_id={iid}&node_id={nid}&match_no={N}
```
Response (key fields, observed on a cleared match):
```json
{
  "ok": true,
  "match": { "ended": true, "winner_side": "ally", ... },
  "me": { "user_id": 150996, "in_match": false, "alive": false, "tokens": 0, "skills": [] },
  "teams": {
    "ally":  { "players_by_num": { "1": {...player...}, "2": {...}, ..., "5": {...} } },
    "enemy": { "players_by_num": { "1": {...npc...},     "2": {...}, ..., "5": {...} } }
  },
  "room_joined": false,
  "room_slot": 0,
  "room_status": "cleared",
  "match_no": 1
}
```

A populated ally slot looks like:
```json
{ "key": "ally:22387", "user_id": 22387, "npc_id": 0,
  "username": "[DLU] TheHaser", "role": "Cleric",
  "hp": 939555, "hp_max": 940000, "alive": true, "slot": 1, ... }
```

Confirmed `room_status` values (probed against fresh instance 7135):
- `'open'` — lobby is open, `< 5` joined, `match.ended == False`. Joinable.
- `'live'` — 5 joined and the trial is running, `match.ended == False`. Not joinable (full).
- `'cleared'` — resolved, `match.ended == True`. Skip.

**Empty-slot encoding:** `players_by_num` uses **two different encodings interchangeably** for empty slots — sometimes the slot is present with the literal `null` value (e.g. `"5": null`), sometimes the slot key is **omitted entirely** from the dict. Both mean "no one in that seat." The picker must therefore iterate `slot in 1..5` and check `ally.get(str(slot)) is None` (which Python returns for both missing keys and `null` values). Defensive fallback: also treat a present-but-zeroed entry (`user_id == 0 AND npc_id == 0`) as empty. This was verified on instance 7135: match 12 had slot 5 as `null`, while match 2 (3/5) simply omitted slots 2 and 5.

### 3.3 Join a match (cube source)
**The action for `source=cube` is `pick_slot`, not `join_room`.** `join_room` is the path for non-cube sources (e.g. events) and returns `"Unknown action."` against the cube. Verified live on instance 7135 / match 12.

```
POST /pvp_style_action.php
  source=cube
  instance_id={iid}
  node_id={nid}
  match_no={N}
  action=pick_slot
  slot_index={1..5}
```

To pick `slot_index`, first GET `pvp_style_state.php` and find any slot in `teams.ally.players_by_num` whose value is `null` — that's an empty seat. Any of them works (server doesn't care which empty slot we claim).

When the chosen slot was the **last** one needed, the server replies with:
```json
{ "ok": true, "message": "Formation filled. The match is now live." }
```
…and the match transitions from `room_status='open'` to `'live'` immediately.

Other actions on the same endpoint we may eventually use:
- `leave_room` — abandon a slot before the trial starts (TBD whether the server allows this once joined)
- `start_trial` / `restart_trial` — manual launch (server auto-launches on a 5/5 fill, so we shouldn't need this)
- `surrender` — bail out of an in-progress match
- `use_skill`, `pick_slot` — in-fight actions (we'll let the auto-AI handle skills; we're only here for the join + stamina potion reward)

---

## 4. Picker algorithm (PvP)

For each target PvP room (`ring_ward`, `duel_heart`, and once unlocked `tyrant_conclave`):

1. Land on cube page; resolve current `instance_id` for The Polyhedral Crucible from `guild_dash.php`.
2. Look up the node by `key` in the embedded `STATE.nodes`. Skip the room if:
   - `node.status == 'hidden'` — the dungeon hasn't unlocked this room yet (e.g. on a fresh instance Duel Heart starts as `hidden`; it appears to reveal after Ring Ward progresses), **or**
   - `node.is_cleared == 1` — the entire node is done for this instance.
3. POST `enter_node` for that node (lands us on `pvp_style_node.php`).
4. **One GET on `pvp_style_node.php`** — parse every `<div class="match">` card and read `Match #N / Slots X/5 <STATUS_LABEL>`. This is the picker's bulk read; we do not poll the per-match state endpoint here.
5. From the parsed cards, build the candidate list:
   - **Joinable** = `STATUS_LABEL == 'OPEN'` (i.e. `room_status == 'open'`) AND `slots < 5`.
   - Skip `LIVE` (full) and `CLEARED`.
6. **Already-in guard.** Before calling `join_room`, GET `pvp_style_state.php` for the chosen candidate and verify `room_joined == False`. Belt-and-suspenders: if `room_joined == True` for that candidate, scan a few more candidates (or skip the whole room — the server enforces 1 match per room per player anyway).
7. **Preference order:**
   - Among joinable candidates with `1 ≤ slots ≤ 4`, pick the one with the **highest** `slots` (ties → lowest `match_no`).
   - If none, pick the lowest-`match_no` candidate with `slots == 0`.
   - If still none, do nothing for this room this cycle.
8. From `pre_state.teams.ally.players_by_num`, find any empty slot — for `slot in 1..5`, empty iff `ally.get(str(slot)) is None` (handles both `null` values and missing keys; see Section 3.2 on the dual encoding). Pick the lowest empty slot as `slot_index`.
9. POST `pvp_style_action.php action=pick_slot` with `source=cube, instance_id, node_id, match_no, slot_index`. Stop after one join per room per cycle.

**Server response when our slot is the 5th/last:**
```json
{ "ok": true, "message": "Formation filled. The match is now live." }
```
…and the match transitions from `room_status='open'` to `'live'` immediately.

### 4.1 Scheduling / cadence (PvP worker)

The PvP guild worker is a **periodic scheduler**, not a long-running loop:

- **Default tick:** every **30 min** the worker wakes up, runs the picker for each target room, and sleeps again.
- **Tighter scheduling near a known cooldown / timer:** if at any point the worker observes that a relevant timer (per-player cooldown, match start countdown, etc.) will expire **in less than 30 min**, it schedules the *next* run for `expiry_time + 1 min` instead of the standard 30-min tick. This way we wake up exactly once just after the gate opens — never early (wasted call), never late by more than ~1 min.
- **Idempotent cycle:** because of the "already joined" hard rule above, re-entering the cycle after a join is harmless — the worker will see `room_joined == True` and skip the room.
- **Site-down behavior:** same pattern as other workers — respect `GameClient.is_site_down`, fall through to `wait_for_site_up`, then resume on the original schedule.

Source of the timer is still TBD (see Section 7 question 7). The worker's scheduler should be written so that timer extraction is a single function (`compute_next_wakeup(now, observed_state) -> datetime`) that we can adjust as we learn what the game exposes.

---

## 5. Army room — flow

Same contract for **Veil Post** (node 5), **Captain Spine** (node 6), and the **Abyssal Muster** boss (node 11). Only `node_id` and `face_key` differ.

> **Goal of the army worker:** participate in each fight just enough to earn the participation reward (a Shadow EXP Scroll), then leave. Minimum damage, maximum throughput across the four fights per node + the boss.

### 5.1 Room landing page
```
GET /guild_dungeon_cube_army_enter.php?instance_id={iid}&node_id={nid}
```
JS-driven page (title is the room name, e.g. `Abyssal Muster`). Polls `guild_dungeon_cube_army_action.php` once per second.

### 5.2 State poll
```
POST /guild_dungeon_cube_army_action.php
  action=state
  instance_id={iid}
  node_id={nid}
```
Response (observed against `Abyssal Muster`, all-cleared):
```json
{
  "ok": true,
  "cards": [
    {
      "run_id": 5406,
      "match_no": 1,
      "battle_id": 9438,
      "status": "cleared",
      "winner_side": "ALLY",
      "banner_image": "/images/army/.../Fenraxx the Moon-Eater.webp",
      "banner_name": "Fenraxx the Moon-Eater",
      "banner_power": "The Radiant Hammer",
      "captains": [ {...}, {...}, {...} ],
      "participant_count": 33,
      "total_damage": 1968815,
      "total_kills": 21,
      "attackers_preview": [ {...}, ... ]
    }
  ],
  "active_match_no": 0,
  "required_matches": 1,
  "cleared_matches": 1
}
```
Each `cards[].status` is `'cleared'` here. Live values (still unconfirmed in JSON) likely include `'active'` / `'open'` — the JS in the room page treats anything non-cleared as actionable and reads `[data-enter-match]` buttons.

`required_matches` reflects the room size:
- Veil Post / Captain Spine: `required_matches=4`
- Abyssal Muster (boss): `required_matches=1`

### 5.3 Contributors of a single match
```
POST /guild_dungeon_cube_army_action.php
  action=contributors
  instance_id={iid}
  node_id={nid}
  match_no={N}
```
Returns the list of who joined that fight (used by the picker to bias toward fights that already have people in them).

### 5.4 Open a fight (cube → battle page hand-off)
```
POST /guild_dungeon_cube_army_action.php
  action=enter_fight
  instance_id={iid}
  node_id={nid}
  match_no={N}
```
Response (verified on Abyssal Muster instance 7135 / match 1):
```json
{
  "ok": true,
  "redirect": "shadow_army_live_battle.php?battle_id=9564",
  "battle_id": 9564,
  "match_no": 1
}
```
This call **does not** join us yet — it just opens the battle page. We still have to call `join_battle` once on the battle page itself.

### 5.5 The shadow-army battle page — `shadow_army_live_battle.php`

Verified contracts. Action endpoint is the same URL (`shadow_army_live_battle.php`), POST, `application/x-www-form-urlencoded`, body `action=...&...`. Returns `{ok: bool, message?: str, state?: {...}}`.

The page bootstraps `const initialState = {...}` inline; subsequent calls return updated `state`. Key fields on `state`:
- `state.battle.{id, status, winner_side}` — `status` is `'ACTIVE'` while live, something else (`'CLEARED'`?) when resolved.
- `state.viewer.{has_roster, can_join, other_active_match_no, enemy_units_killed}` — on first read for an account that hasn't joined yet, `has_roster` may be `false`. **After a successful `join_battle`, `has_roster` flips to `true` and `can_join` to `false`.** `enemy_units_killed` is the per-player participation gate — *any* value > 0 means we earned the reward.
- `state.captains[]` — every captain in the battle (allies + enemies). Filter to ours via `captain.is_mine == true`.
- `state.engagements[]` — active fights between captains.

#### 5.5.1 Join with my army
```
POST /shadow_army_live_battle.php
  action=join_battle
  battle_id={N}
```
Server picks up to 5 captains from our Shadow Army roster and inserts them into the battle. Response includes the updated `state`. The dev account joined with 1 captain (Malreth, attack 3362); a fully-rostered account would join with 4-5.

#### 5.5.2 Assign a target (= start dealing damage)
```
POST /shadow_army_live_battle.php
  action=assign_target
  battle_id={N}
  attacker_captain_unit_id={my captain id}
  defender_captain_unit_id={enemy captain id}
```
Pick any one of our captains (`is_mine=true, is_dead=false, retreat_requested=false`) and any alive enemy captain (`side='ENEMY', is_dead=false`). Response: `{ok: true, message: "Target assigned.", state}`. An entry appears in `state.engagements` immediately with `status='ACTIVE'`. Damage processes server-side automatically as turns tick; we do not need to drive each swing.

#### 5.5.3 Retreat (= leave the fight, keep the participation credit)
```
POST /shadow_army_live_battle.php
  action=retreat_captain
  battle_id={N}
  captain_unit_id={my captain id}
```
Response: `{ok: true, message: "Retreat queued. It will happen after the next enemy hit.", state}`. The captain's `retreat_requested` flips to `true`. **The retreat is queued — meaning at least one engagement round still plays out before the captain leaves**, which guarantees ≥1 attack from our side. This is exactly what we want: minimum damage, ≥1 hit landed.

> **Important — retreat all our captains, not just the one we assigned a target to.** Each `is_mine` captain stays in the battle until explicitly retreated. To fully leave we issue `retreat_captain` for every captain in `state.captains` where `is_mine == true && is_dead == false && retreat_requested == false`.

Other actions on the same endpoint (not used by the worker, but mapped from JS): `get_attackers` (read-only contributors list).

### 5.6 Picker / scheduler for army rooms

User intent (verbatim): *"we just need to hit as little as possible over 0 and then we can leave the fight and still get the rewards"* and *"once we joined a fight we can't join the same one or other ones so we wait for that one to die"*.

Process army nodes **strictly in order**, one fight per cycle (the server enforces "one match at a time per player", so attempting more is wasted):

1. `Veil Post` (node 5, face=back)
2. `Captain Spine` (node 6, face=back) — only after Veil Post is fully cleared (`is_cleared=1`).
3. `Abyssal Muster` (node 11, face=bottom) — boss, only after BOTH Veil Post and Captain Spine are cleared (its `unlock_rule == 'all_army_other'`).

Per cycle (every 30 min):

1. Re-fetch cube `STATE` and look up each node by `key`.
2. For each node in the order above:
   - **Skip** if `node.status == 'hidden'` OR `node.is_cleared == 1`. Move to next node.
   - POST `enter_node` (cube action) → confirms the node opens (we don't need the redirect URL beyond confirmation).
   - POST `action=state` on `guild_dungeon_cube_army_action.php` for this node → read `cards[]`.
   - **Pick the lowest `match_no`** whose `status` is not `'cleared'` (typically the first non-cleared in numerical order). If none, skip the node.
   - POST `action=enter_fight` for that `match_no` → get `battle_id`.
   - On the battle page, GET / fetch initial `state` (or just inspect the `enter_fight` response if it carries it; otherwise the GET on `shadow_army_live_battle.php?battle_id=X` will). If `state.viewer.other_active_match_no != 0` OR any `is_mine` captain already exists in `state.captains`, **we are already in another match — stop processing army rooms this cycle.**
   - Otherwise: POST `action=join_battle`. If `ok: false` (server rejection — usually "already in another match" or the match auto-closed), log and stop processing army rooms this cycle.
   - Pick our captain: any `is_mine == true && is_dead == false && retreat_requested == false`. Pick the enemy: any `side == 'ENEMY' && is_dead == false`. POST `action=assign_target` with both ids.
   - **Immediately** (no sleep) POST `action=retreat_captain` for each `is_mine == true && is_dead == false && retreat_requested == false` captain. The retreat is queued for "after the next enemy hit", so our captain still gets ≥1 attack in but we walk away with minimum exposure (~1 swing's damage instead of 61k from a 15-second wait).
   - Stop the cycle — we're done for the next 30 min, regardless of how many other army nodes / fights remain (server lock will reject further joins anyway).

3. Sleep 30 min, loop.

### 5.7 Damage budget — verified

| Approach | Damage dealt | Notes |
|---|---|---|
| `assign_target` → sleep 15s → `retreat_captain` | **61,643** | What we did on the first probe — way too much |
| `assign_target` → `retreat_captain` immediately | ≤ 1 captain swing (≪ 5k) | Recommended; retreat queue still guarantees ≥1 attack |

We never need to wait for `enemy_units_killed > 0` before retreating — the retreat queue guarantees the swing happens server-side. The bot can fire `assign_target` and `retreat_captain` back-to-back with zero in-between sleep.

---

## 6. Implementation sketch (where this fits in the codebase)

Following the existing pattern (CLAUDE.md → "Common Tasks → Add a new automation worker"):

- **`veyra/game/endpoints.py`** — add:
  ```python
  GUILD_DUNGEON_DASH_URL  = f"{BASE_URL}/guild_dash.php"
  GUILD_DUNGEON_CUBE_URL  = f"{BASE_URL}/guild_dungeon_cube.php"
  GUILD_DUNGEON_CUBE_ACTION_URL = f"{BASE_URL}/guild_dungeon_cube_action.php"
  PVP_STYLE_NODE_URL    = f"{BASE_URL}/pvp_style_node.php"
  PVP_STYLE_STATE_URL   = f"{BASE_URL}/pvp_style_state.php"
  PVP_STYLE_ACTION_URL  = f"{BASE_URL}/pvp_style_action.php"
  ARMY_ENTER_URL        = f"{BASE_URL}/guild_dungeon_cube_army_enter.php"
  ARMY_ACTION_URL       = f"{BASE_URL}/guild_dungeon_cube_army_action.php"
  ```
- **`veyra/game/parser.py`** — `parse_open_dungeons(html)` (returns list of `(name, instance_id)`) and `parse_cube_state(html)` (extracts the inline `const STATE = {...}` JSON via regex).
- **`veyra/game/client.py`** — thin wrappers: `fetch_open_dungeons()`, `fetch_cube_state(iid)`, `enter_cube_node(iid, nid, face_key)`, `fetch_pvp_match_state(iid, nid, match_no)`, `pvp_join_room(iid, nid, match_no)`.
- **`veyra/engine/dungeon_pvp_farmer.py`** — new worker implementing the picker in section 4. Mirrors `pvp_fighter.py` in shape (state dataclass, async loop, log lines, integrates with `AccountManager.is_site_down`).
- **`veyra/engine/account_manager.py`** — wire `start_dungeon_pvp() / stop_dungeon_pvp() / get_dungeon_pvp_status()`.
- **`veyra/api/routes.py`** — `POST /api/dungeon-pvp/start`, `POST /api/dungeon-pvp/stop`, `GET /api/dungeon-pvp/status`.
- **`veyra/web/`** — add a Guild tab to the dashboard with start/stop + last-action log.

Army worker (`dungeon_army_farmer.py`) follows the same scaffolding once we capture a live army-room state.

---

## 7. Open questions / blockers

Resolved by the 2026-05-03 fresh-instance probe (instance 7135):

- ✅ **Empty-slot shape:** confirmed `null` in `players_by_num`.
- ✅ **`room_status` "open" literal:** confirmed `'open'`. Full set is `'open' | 'live' | 'cleared'`.
- ✅ **Live `match.ended` value:** confirmed `False` for both `'open'` and `'live'`; `True` only when `'cleared'`.
- ✅ **`node_id` stability:** confirmed — instance 7135 has the same `(key → id, face_key)` mapping as instance 7081 (Ring Ward=7/right, Duel Heart=8/right, Veil Post=5/back, Captain Spine=6/back, Abyssal Muster=11/bottom, Tyrant Conclave=9/bottom).
- ✅ **Bulk read possible:** the node landing page renders all 20 match cards with status + `slots/5` text, so the picker reads them in **one** GET instead of 20 state polls.

Still open:

0. **Undercroft unlock semantics.** Same endpoints as the non-boss rooms (confirmed for Abyssal Muster). Still TBD: which `node.status` literal the boss carries when locked vs unlocked. On fresh instance 7135 we observed `status='hidden'` for unrevealed PvP rooms (Duel Heart) — the boss probably uses the same `'hidden'` until its precondition fires. Verify when the prior rooms in this instance clear.
1. **Duel Heart visibility chain.** On a fresh instance Duel Heart starts as `node.status == 'hidden'`. We assumed both Arena Bastion rooms were always available. Need to capture the moment Duel Heart reveals to confirm whether it's tied to Ring Ward being cleared, partially cleared, or another trigger.
2. **Live Army fight JSON & battle-page actions.** Captured the `state` shape for cleared matches but not for `'active'`/`'open'` ones. Re-probe an army node on this fresh instance once a fight is running.
3. ✅ **Per-player cooldown timer source — RESOLVED.** Verified post-resolution on 2026-05-03: the cooldown surfaces as **plain text on `pvp_style_node.php`** once the player's previous match in that node has resolved. Format:
   ```
   You are currently locked to another match in this node for about HH:MM:SS more.
   You can still re-enter that match now.
   ```
   Regex: `r"locked to another match in this node for about (\d+):(\d{2}):(\d{2}) more"` → seconds = h*3600 + m*60 + s. The per-player cooldown is **~2 hours** measured from match resolution (not from join), confirmed by reading `01:56:31` remaining about 4 minutes after our match cleared.

   This text is **only present after the match has resolved** — during the live phase the page just shows the normal match grid. The picker's adaptive scheduler (Section 4.1) should:
   1. After every successful `pick_slot`, remember the (`instance_id`, `node_id`, `match_no`) we joined.
   2. On every wake, fetch `pvp_style_node.php` for that node and regex out the cooldown if present → schedule next wake at `now + cooldown + 60s`.
   3. If the text is absent and we previously had a cooldown for this node → it's expired; resume normal 30-min ticks.

   Notes / corner cases:
   - The locked-to-match line implies we *can* still re-enter the same match (e.g. to spectate or chat) but cannot pick a slot in any other match in that node until the cooldown lapses. Confirms the "one match per node per player" rule.
   - The cooldown is per-node, not per-room — Ring Ward and Duel Heart will have independent cooldowns once Duel Heart unlocks.
   - Across-instances: open question (probably resets when a new dungeon instance opens, but worth verifying when the current Crucible expires).

---

## 8. Probe artifacts (kept for reference)

Standalone scripts used for discovery; safe to delete once the worker is implemented.

| Path | Purpose |
|---|---|
| `/tmp/probe_guild.py` | Login → fetch `game_dash.php` and `guild_dash.php`, locate Guild link + Open Dungeons cards |
| `/tmp/probe_cube.py` | Fetch `guild_dungeon_cube.php`, dump `FACE_DATA` + `STATE`, locate target nodes by name |
| `/tmp/probe_enter.py` | Fire `enter_node` for one PvP and one Army node, follow the redirect, dump the room page |
| `/tmp/probe_match.py` | Hit `pvp_style_state.php` and inspect the per-match JSON shape |

Sample HTML / JSON saved in `/tmp/`:
- `guild_dash.html` — Open Dungeons cards
- `guild_dungeon_cube.html` — cube page with `STATE`
- `room_ring_ward_pvp.html` — PvP room landing
- `room_captain_spine_army.html` — Army room landing
- `match_ring_1.html` — a single PvP match page (cleared)
- `match_state.json` — `pvp_style_state.php` response (cleared)

Dev creds for the probe live in the deployed Hetzner `.env` (see memory `reference_dev_credentials.md`).

---

## 9. Next steps when the dungeon resets

1. Re-run `probe_cube.py` with the new `instance_id` → confirm `node_id` mapping is unchanged.
2. Run `probe_match.py` against a `match_no` whose `room_status != 'cleared'` to capture:
   - the empty-slot shape,
   - the exact non-`'cleared'` `room_status` literal,
   - a `match.ended=false` object.
3. Update sections 3.2, 4 (step 4) and 7 with the confirmed values.
4. Implement the worker per section 6 and ship the Guild tab in the UI.
