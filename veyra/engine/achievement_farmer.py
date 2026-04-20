"""Event-achievement farmer.

Farms damage-per-mob achievements (e.g. "Deal at least 3,000,000 damage to
1,000 Arcaneback Bears") for a single event wave. The farmer:

  1. Scrapes /achievements.php, filtering to achievements whose monster is
     present on the configured event wave and whose kill count < target.
  2. Builds one TargetConfig per achievement in page order and hands them to
     the shared wave_worker, which already kills every alive instance of
     target #1 before moving to #2 (and revisits #1 after a respawn tick).
  3. Re-polls progress periodically; when an achievement hits its target or
     a new one unlocks, it rebuilds the target list.

Stamina recovery (smart loot + potion fallback) and rate-limit backoff are
inherited from wave_worker unchanged.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from veyra.engine.rate_limiter import RateLimiter
from veyra.engine.wave_farmer import FarmerState, worker as wave_worker
from veyra.game.client import GameClient
from veyra.game.types import TargetConfig

logger = logging.getLogger("veyra.achievements")

DEFAULT_EVENT_WAVE = 101
DEFAULT_STAMINA_LABEL = "10 Stamina"
POLL_EVERY_SECONDS = 30


@dataclass
class AchievementState(FarmerState):
    """Extends FarmerState so it plugs into the shared worker mechanics."""
    wave: int = DEFAULT_EVENT_WAVE
    stamina_label: str = DEFAULT_STAMINA_LABEL
    # All damage-per-mob achievements parsed from the page (page order).
    achievements: list[dict] = field(default_factory=list)
    # Subset currently farmable (monster is on the wave + not yet complete).
    active: list[dict] = field(default_factory=list)
    # Names present on the configured wave (informational, for the UI).
    wave_monsters: list[str] = field(default_factory=list)
    current_monster: str = ""
    last_poll_ts: float = 0.0


async def _poll_achievements(game: GameClient, state: AchievementState) -> bool:
    """Refresh achievements + which subset is farmable on the configured wave."""
    try:
        all_ach = await game.fetch_achievements()
    except Exception as e:
        state.log(f"  Achievements poll failed: {e}")
        return False
    if not all_ach:
        state.log("  No damage-per-mob achievements parsed from page")
        return False

    try:
        mobs = await game.fetch_wave(state.wave)
        wave_names = sorted({m.name for m in mobs})
    except Exception as e:
        state.log(f"  Fetch wave {state.wave} failed: {e}")
        return False

    state.achievements = all_ach
    state.wave_monsters = wave_names
    wave_set = set(wave_names)
    state.active = [
        a for a in all_ach
        if a["monster"] in wave_set and a["kills_current"] < a["kills_required"]
    ]
    state.last_poll_ts = time.time()
    return True


def _build_targets(state: AchievementState) -> list[TargetConfig]:
    """Preserve achievement page order → priority 1..N. Dedupe per mob (an
    achievement maps 1:1 to a mob, but guard anyway)."""
    targets: list[TargetConfig] = []
    seen: set[str] = set()
    priority = 0
    for a in state.active:
        mob = a["monster"]
        if mob in seen:
            continue
        seen.add(mob)
        priority += 1
        targets.append(TargetConfig(
            name=mob,
            wave=state.wave,
            damage_goal=a["damage_required"],
            stamina=state.stamina_label,
            priority=priority,
        ))
    return targets


async def achievement_worker(
    game: GameClient,
    state: AchievementState,
    limiter: RateLimiter,
) -> None:
    state.stats.started_at = time.time()
    state.log("")
    state.log(f"=== Starting Achievement Farmer (wave {state.wave}) ===")

    if not await _poll_achievements(game, state):
        state.log("Initial achievements poll failed — aborting.")
        state.stop()
        return

    if state.wave_monsters:
        state.log(f"  Wave {state.wave} mobs: {', '.join(state.wave_monsters)}")
    if not state.active:
        state.log("No farmable achievements on this wave "
                  "(either all complete or no monster match).")
        state.stop()
        return

    try:
        while state.running:
            if not state.active:
                state.log("")
                state.log("=== All wave-available achievements complete ===")
                break

            targets = _build_targets(state)
            state.current_monster = state.active[0]["monster"]

            state.log("")
            state.log(f"→ Farming {len(state.active)} achievement(s) in page order:")
            for a in state.active:
                state.log(
                    f"  [{a['monster']}] {a['title']}: "
                    f"{a['kills_current']}/{a['kills_required']} "
                    f"(req {a['damage_required']:,} dmg each)"
                )

            start_counts = {a["title"]: a["kills_current"] for a in state.active}

            nested = FarmerState()
            nested.running = True
            nested.stats.started_at = state.stats.started_at
            last_seen = nested._log_id

            farm_task = asyncio.create_task(
                wave_worker(game, targets, nested, limiter, recheck_priority=True)
            )

            try:
                while not farm_task.done() and state.running:
                    for entry in nested.logs:
                        if entry["id"] > last_seen:
                            last_seen = entry["id"]
                            state.log(entry["msg"])

                    if time.time() - state.last_poll_ts >= POLL_EVERY_SECONDS:
                        if await _poll_achievements(game, state):
                            completed = [
                                a for a in state.active
                                if a["kills_current"] >= a["kills_required"]
                            ]
                            gained_any = False
                            for a in state.active:
                                prev = start_counts.get(a["title"], a["kills_current"])
                                delta = a["kills_current"] - prev
                                if delta > 0:
                                    gained_any = True
                                    state.log(
                                        f"  [poll] {a['title']}: "
                                        f"{a['kills_current']}/{a['kills_required']} "
                                        f"(+{delta})"
                                    )
                            if not gained_any:
                                state.log("  [poll] no kill-credit yet")
                            if completed:
                                titles = ", ".join(a["title"] for a in completed)
                                state.log(f"  ✓ complete: {titles} — rebuilding targets")
                                nested.stop()
                                break

                    state.stats.killed = nested.stats.killed
                    state.stats.damage = nested.stats.damage
                    state.stats.stamina_spent = nested.stats.stamina_spent
                    state.stats.looted = nested.stats.looted
                    state.stats.monsters_attacked = nested.stats.monsters_attacked

                    await asyncio.sleep(1)
            finally:
                if not farm_task.done():
                    nested.stop()
                    try:
                        await asyncio.wait_for(farm_task, timeout=30)
                    except (asyncio.TimeoutError, Exception):
                        farm_task.cancel()
                        try:
                            await farm_task
                        except (asyncio.CancelledError, Exception):
                            pass

                for entry in nested.logs:
                    if entry["id"] > last_seen:
                        last_seen = entry["id"]
                        state.log(entry["msg"])

            if not state.running:
                break
            if not nested.running:
                prev_counts = dict(start_counts)
                await _poll_achievements(game, state)
                progressed = any(
                    a["kills_current"] > prev_counts.get(a["title"], 0)
                    for a in state.active
                )
                if not progressed:
                    state.log("Run ended with no progress — stamina exhausted.")
                    break

        state.log("")
        state.log("=== Achievement Farmer stopped ===")
    except Exception as e:
        logger.error("Achievement worker fatal: %s", e, exc_info=True)
        state.log(f"Fatal: {e}")
    finally:
        state.current_monster = ""
        state.stop()
