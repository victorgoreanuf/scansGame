"""Event-collection farmer.

Farms the bulk-material ingredients required to complete an event collection
(or the blacksmith recipes that feed into one). The farmer:

  1. Polls /collections.php for live `have` counts.
  2. Picks the most-lacking item, selects the cheapest-dmg source monster,
     builds a single TargetConfig, and runs the existing wave_farmer worker
     (which handles attack, rejoin, rate-limit backoff, smart loot + potion
     fallback on stamina exhaustion) for one round.
  3. Re-polls progress and repeats until every required item hits its target
     or stamina + potions + level-up loot are all exhausted.

Boss-only drops (Hollow Star Avatar materials) are excluded — the user is
expected to farm those manually.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from veyra.engine.rate_limiter import RateLimiter
from veyra.engine.wave_farmer import FarmerState, worker as wave_worker
from veyra.game.client import GameClient
from veyra.game.types import TargetConfig

logger = logging.getLogger("veyra.collection")


# ── Plans ────────────────────────────────────────────────────────────────────

EVENT_WAVE = 101                  # key in WAVE_MAP for event 8 wave 101
DEFAULT_STAMINA_LABEL = "10 Stamina"
POLL_EVERY_SECONDS = 12           # re-check progress at most once per interval

# Per-mob damage floor — must be high enough to also earn the Emberfall Token
# roll (3 M dmg on every event-8 mob).
MIN_DAMAGE_PER_MOB = 3_000_000

# Collection ID → plan. Only collections that the bot is capable of farming
# (i.e. whose materials drop from normal event mobs, not bosses) are listed.
COLLECTION_PLANS: dict[int, dict] = {
    17: {
        "name": "Ashscript Regalia",
        "reward": "+50 attack",
        "image": "https://demonicscans.org/images/events/Emberfall/items/ashscript_staff.webp",
        "items": {
            "Ashscript Staff": 1000,
            "Ashscript Hood":  1000,
            "Ashscript Robe":  1000,
            "Ashscript Gloves": 1000,
            "Ashscript Boots": 1000,
        },
    },
    18: {
        "name": "Vaelith's Final Testament",
        "reward": "+100 attack",
        "image": "https://demonicscans.org/images/events/Emberfall/items/Vaelith_Last_Testament.webp",
        # 2000 of each Ashscript item feeds the 5 legendary forge recipes.
        # Boss materials (Star-Split Glass Heart, Living Black Index, Emberwing
        # Plume, Lucid Memory Shard) are user-farmed.
        "items": {
            "Ashscript Staff": 2000,
            "Ashscript Hood":  2000,
            "Ashscript Robe":  2000,
            "Ashscript Gloves": 2000,
            "Ashscript Boots": 2000,
        },
    },
}

# Cheapest-dmg monster on event wave 101 for each farmable ingredient.
# (All are 100% drops; thresholds from loot_db_event_8_w101.json.)
BEST_SOURCES: dict[str, dict] = {
    "Ashscript Hood":   {"monster": "Arcaneback Bear",     "wave": EVENT_WAVE, "dmg_required": 2_400_000},
    "Ashscript Gloves": {"monster": "Arcanefang Wolf",     "wave": EVENT_WAVE, "dmg_required": 2_500_000},
    "Ashscript Boots":  {"monster": "Arcanecrest Hyena",   "wave": EVENT_WAVE, "dmg_required": 2_500_000},
    "Ashscript Staff":  {"monster": "Runestag",            "wave": EVENT_WAVE, "dmg_required": 2_600_000},
    "Ashscript Robe":   {"monster": "Hexpyre Crow",        "wave": EVENT_WAVE, "dmg_required": 2_600_000},
}


def plannable_collections() -> list[dict]:
    """Return a serializable list of plannable collections (for API / UI)."""
    return [
        {
            "id": cid,
            "name": plan["name"],
            "reward": plan["reward"],
            "image": plan.get("image", ""),
            "items": [
                {
                    "name": n,
                    "need": need,
                    "source_monster": BEST_SOURCES.get(n, {}).get("monster", ""),
                    "source_wave": BEST_SOURCES.get(n, {}).get("wave", 0),
                }
                for n, need in plan["items"].items()
            ],
        }
        for cid, plan in COLLECTION_PLANS.items()
    ]


# ── State ────────────────────────────────────────────────────────────────────


@dataclass
class CollectionState(FarmerState):
    """Extends FarmerState so it plugs into wave_farmer + smart_loot unchanged."""
    collection_id: int = 0
    collection_name: str = ""
    stamina_label: str = DEFAULT_STAMINA_LABEL
    # Progress mirrored from /collections.php (name → (have, need))
    progress: dict[str, dict] = field(default_factory=dict)
    current_item: str = ""
    last_poll_ts: float = 0.0


# ── Worker ───────────────────────────────────────────────────────────────────


async def _poll_progress(game: GameClient, state: CollectionState) -> bool:
    """Refresh state.progress from /collections.php. Returns True on success."""
    try:
        data = await game.fetch_collection_progress(state.collection_id)
    except Exception as e:
        state.log(f"  Progress poll failed: {e}")
        return False
    if not data:
        state.log(f"  Collection {state.collection_id} not found on page")
        return False

    plan = COLLECTION_PLANS[state.collection_id]
    new_progress: dict[str, dict] = {}
    for item in data["items"]:
        name = item["name"]
        target = plan["items"].get(name, item["need"])
        new_progress[name] = {
            "have": item["have"],
            "need": target,
            "game_need": item["need"],
            "image": item["image"],
        }
    state.progress = new_progress
    state.last_poll_ts = time.time()
    return True


def _farmable_items_sorted(state: CollectionState) -> list[str]:
    """Return names of still-needed farmable items, ordered by have ascending
    (rarest-in-inventory first)."""
    items = [
        (name, p["have"])
        for name, p in state.progress.items()
        if name in BEST_SOURCES and p["have"] < p["need"]
    ]
    items.sort(key=lambda x: x[1])
    return [name for name, _ in items]


def _build_targets(state: CollectionState) -> list[TargetConfig]:
    """Build one TargetConfig per still-needed farmable item, priority-ordered
    by lowest inventory count. The wave worker then iterates all targets per
    round, skipping mobs already at their per-item threshold — so when one
    item's mobs are exhausted we immediately move to the next instead of
    sleeping out a respawn cycle."""
    targets: list[TargetConfig] = []
    for priority, name in enumerate(_farmable_items_sorted(state), 1):
        src = BEST_SOURCES[name]
        # Per-mob floor = max(item drop threshold, Emberfall Token threshold 3M).
        dmg_threshold = max(src["dmg_required"], MIN_DAMAGE_PER_MOB)
        targets.append(
            TargetConfig(
                name=src["monster"],
                wave=src["wave"],
                damage_goal=dmg_threshold,
                stamina=state.stamina_label,
                priority=priority,
            )
        )
    return targets


def _all_farmable_done(state: CollectionState) -> bool:
    """True iff every farmable item has met its target."""
    for name, p in state.progress.items():
        if name not in BEST_SOURCES:
            continue
        if p["have"] < p["need"]:
            return False
    return True


async def collection_worker(
    game: GameClient,
    state: CollectionState,
    limiter: RateLimiter,
) -> None:
    """Main loop. Delegates per-item farming to the wave_farmer worker."""
    plan = COLLECTION_PLANS.get(state.collection_id)
    if not plan:
        state.log(f"Unknown collection id {state.collection_id}")
        state.stop()
        return

    state.collection_name = plan["name"]
    state.stats.started_at = time.time()
    state.log("")
    state.log(f"=== Starting collection farm: {plan['name']} ({plan['reward']}) ===")

    # Initial progress poll
    if not await _poll_progress(game, state):
        state.log("Initial progress poll failed — aborting.")
        state.stop()
        return

    try:
        while state.running:
            if _all_farmable_done(state):
                state.log("")
                state.log("=== All farmable items complete! ===")
                for n, p in state.progress.items():
                    marker = "✓" if p["have"] >= p["need"] else "·"
                    state.log(f"  {marker} {n}: {p['have']:,} / {p['need']:,}")
                break

            targets = _build_targets(state)
            if not targets:
                state.log("No farmable items left with deficit — stopping.")
                break

            # Primary item for UI = lowest-have (targets are already sorted).
            active_items = _farmable_items_sorted(state)
            state.current_item = active_items[0]

            state.log("")
            state.log(f"→ Farming {len(active_items)} item(s), rarest first:")
            for t, name in zip(targets, active_items):
                p = state.progress[name]
                state.log(
                    f"  [{t.priority}] {name}: {p['have']:,}/{p['need']:,} "
                    f"via {t.name} (≥{t.damage_goal:,} dmg)"
                )

            start_have = {n: state.progress[n]["have"] for n in active_items}

            nested_state = FarmerState()
            nested_state.running = True
            nested_state.stats.started_at = state.stats.started_at
            original_log_id = nested_state._log_id

            farm_task = asyncio.create_task(
                wave_worker(game, targets, nested_state, limiter)
            )
            last_seen_nested_log_id = original_log_id

            try:
                while not farm_task.done() and state.running:
                    # Forward any new logs from nested worker
                    for entry in nested_state.logs:
                        if entry["id"] > last_seen_nested_log_id:
                            last_seen_nested_log_id = entry["id"]
                            state.log(entry["msg"])

                    if time.time() - state.last_poll_ts >= POLL_EVERY_SECONDS:
                        if await _poll_progress(game, state):
                            # Any active item complete? → rebuild target list.
                            completed = [
                                n for n in active_items
                                if state.progress[n]["have"] >= state.progress[n]["need"]
                            ]
                            gained_any = False
                            for n in active_items:
                                gained = state.progress[n]["have"] - start_have[n]
                                if gained > 0:
                                    gained_any = True
                                    state.log(
                                        f"  [poll] {n} {state.progress[n]['have']:,}"
                                        f"/{state.progress[n]['need']:,} (+{gained})"
                                    )
                            if not gained_any:
                                state.log("  [poll] no inventory gains yet")
                            if completed:
                                state.log(
                                    f"  ✓ complete: {', '.join(completed)} — "
                                    f"rebuilding target list"
                                )
                                nested_state.stop()
                                break

                    # Forward stats snapshots so the UI sees kill/dmg/stam updates
                    state.stats.killed = nested_state.stats.killed
                    state.stats.damage = nested_state.stats.damage
                    state.stats.stamina_spent = nested_state.stats.stamina_spent
                    state.stats.looted = nested_state.stats.looted
                    state.stats.monsters_attacked = nested_state.stats.monsters_attacked

                    await asyncio.sleep(1)
            finally:
                if not farm_task.done():
                    nested_state.stop()
                    try:
                        await asyncio.wait_for(farm_task, timeout=30)
                    except (asyncio.TimeoutError, Exception):
                        farm_task.cancel()
                        try:
                            await farm_task
                        except (asyncio.CancelledError, Exception):
                            pass

                # Drain any trailing logs
                for entry in nested_state.logs:
                    if entry["id"] > last_seen_nested_log_id:
                        last_seen_nested_log_id = entry["id"]
                        state.log(entry["msg"])

            # If the nested worker stopped itself (stamina fully exhausted and
            # neither smart_loot nor potions could recover), we should stop too.
            if not state.running:
                break
            if not nested_state.running:
                # Worker stopped on its own — check why. If no progress was
                # made on any active item, stamina is the likely culprit.
                prev_have = dict(start_have)
                await _poll_progress(game, state)
                progressed = any(
                    state.progress[n]["have"] > prev_have[n] for n in active_items
                )
                if not progressed:
                    state.log("Farming run ended with no progress — stamina exhausted.")
                    break

        state.log("")
        state.log("=== Collection farmer stopped ===")
    except Exception as e:
        logger.error("Collection worker fatal error: %s", e, exc_info=True)
        state.log(f"Fatal: {e}")
    finally:
        state.current_item = ""
        state.stop()
