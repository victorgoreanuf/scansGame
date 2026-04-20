"""Wave farming engine — ported from slasher_app.py worker + farm_monster."""

import asyncio
import logging
from dataclasses import dataclass, field

from veyra.game.client import GameClient
from veyra.game.endpoints import get_stamina_option, step_down_stamina
from veyra.game.types import FarmStats, Monster, StaminaPotion, TargetConfig
from veyra.engine.rate_limiter import RateLimiter

# Avoid circular import — imported at use time
_smart_loot = None

logger = logging.getLogger("veyra.farmer")

RESPAWN_WAIT = 30
REJOIN_EVERY = 20
STEP_UP_EVERY = 10  # every N hits at reduced cost, try original cost again


@dataclass
class FarmerState:
    running: bool = False
    stats: FarmStats = field(default_factory=FarmStats)
    logs: list[dict] = field(default_factory=list)
    _log_id: int = 0

    def log(self, msg: str) -> None:
        self._log_id += 1
        self.logs.append({"id": self._log_id, "msg": msg})
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        logger.info(msg)

    def stop(self) -> None:
        self.running = False


async def farm_monster(
    game: GameClient,
    monster_id: str,
    stamina_label: str,
    goal: int,
    name: str,
    state: FarmerState,
    limiter: RateLimiter,
) -> str:
    """
    Attack a single monster until damage goal is reached.
    Returns: "done" | "stamina" | "error"
    """
    stam = get_stamina_option(stamina_label)
    original_stam = stam
    total = 0
    errors = 0
    zero_streak = 0
    hits = 0
    reduced_hits = 0
    prev_hp = None

    while total < goal and state.running and errors < 5 and not game.is_site_down:
        # Re-join every N hits
        if hits > 0 and hits % REJOIN_EVERY == 0:
            try:
                await game.join_battle(monster_id)
                await asyncio.sleep(1)
            except Exception:
                pass

        # Try stepping back up to original cost after N hits at reduced cost
        if stam["cost"] < original_stam["cost"]:
            reduced_hits += 1
            if reduced_hits >= STEP_UP_EVERY:
                stam = original_stam
                reduced_hits = 0
                state.log(f"    Trying {stam['cost']} Stamina again...")

        await limiter.wait()

        try:
            result = await game.attack(
                monster_id, stam["skill_id"], stam["cost"], prev_hp
            )
        except Exception as e:
            state.log(f"    Error: {e}")
            game.record_net_failure()
            errors += 1
            if game.is_site_down:
                return "error"
            await asyncio.sleep(2)
            continue

        game.record_net_success()

        if result.is_success:
            dmg = result.damage

            if result.monster_hp >= 0:
                prev_hp = result.monster_hp

            if dmg == 0:
                zero_streak += 1
                if zero_streak <= 2:
                    dmg = max(1000, goal // 100)
                    total += dmg
                    state.log(f"    +~{dmg:,} (estimated)")
                else:
                    state.log(f"    Damage still 0 after {zero_streak} hits — skipping")
                    return "error"
            else:
                zero_streak = 0
                # Server may return cumulative total
                if dmg > total:
                    hit_dmg = dmg - total
                    total = dmg
                else:
                    hit_dmg = dmg
                    total += dmg
                state.stats.damage += hit_dmg

            state.stats.stamina_spent += stam["cost"]
            errors = 0
            hits += 1
            reduced_hits = 0  # reset — current cost is working
            limiter.on_success()

            pct = min(100, total * 100 // goal) if goal else 0
            if zero_streak == 0:
                state.log(f"    +{hit_dmg:,}  ({total:,}/{goal:,})  [{pct}%]")

            if result.is_dead:
                state.stats.killed += 1
                state.log(f"    {name} died!")
                return "done"
            if total >= goal:
                state.log("    Goal reached!")
                return "done"

        elif result.is_dead:
            state.log("    Already dead")
            return "done"

        elif result.is_rate_limited:
            state.log("    Rate limited...")
            limiter.on_rate_limit()
            errors += 1
            await asyncio.sleep(3)

        elif result.is_stamina_exhausted:
            next_stam = step_down_stamina(stam["cost"])
            if next_stam:
                stam = next_stam
                state.log(f"    Downgrading to {stam['cost']} Stamina...")
                await asyncio.sleep(1)
                continue
            else:
                state.log("    OUT OF STAMINA (0 left)")
                return "stamina"

        else:
            state.log(f"    Unexpected: {result.message}")
            errors += 1
            await asyncio.sleep(2)

    return "done" if total >= goal else "error"


async def _try_stamina_potion(game: GameClient, state: FarmerState, wave: int = 1) -> bool:
    """Use stamina potions to fill up to max without overfilling.

    For partial potions (e.g. +20): use as many as fit without waste.
    For full-refill potions: use one (always fills to max).
    Returns True if any stamina was restored.
    """
    state.log("")
    state.log("=== Checking inventory for stamina potions ===")

    try:
        player = await game.fetch_player_stats(wave)
    except Exception as e:
        state.log(f"  Failed to fetch player stats: {e}")
        return False

    stamina_needed = player.stamina_max - player.stamina_current
    state.log(f"  Stamina: {player.stamina_current} / {player.stamina_max} (need {stamina_needed})")

    if stamina_needed <= 0:
        state.log("  Stamina already full")
        return True

    try:
        potions = await game.fetch_stamina_potions()
    except Exception as e:
        state.log(f"  Failed to fetch inventory: {e}")
        return False

    if not potions:
        state.log("  No stamina potions in inventory")
        return False

    for p in potions:
        state.log(f"  {p.name} x{p.quantity} ({'+' + str(p.stamina_value) if p.stamina_value else 'full refill'})")

    any_used = False

    # First pass: use partial potions (sorted small→large), skip full-refill.
    # Partials are used even if they'd overfill — Full Stamina Potion is only
    # a last resort when no partial potions remain.
    full_potion: StaminaPotion | None = None
    for potion in potions:
        if potion.quantity <= 0 or stamina_needed <= 0:
            continue

        if potion.is_full:
            # Save full-refill as last resort
            full_potion = potion
            continue
        else:
            # Use as many as fit; allow one extra to cover the remainder (overfill OK)
            exact = stamina_needed // potion.stamina_value
            if stamina_needed % potion.stamina_value > 0:
                exact += 1
            use_count = min(exact, potion.quantity)
            if use_count <= 0:
                continue
            state.log(f"  Using {use_count}x {potion.name} (+{use_count * potion.stamina_value} stamina)...")

            for i in range(use_count):
                try:
                    ok = await game.use_stamina_potion(potion.inv_id)
                except Exception as e:
                    state.log(f"  Error on potion #{i + 1}: {e}")
                    break
                if not ok:
                    state.log(f"  Failed on potion #{i + 1}")
                    break
                any_used = True
                stamina_needed -= potion.stamina_value
                potion.quantity -= 1
                await asyncio.sleep(0.3)

            state.log(f"  Stamina gap remaining: ~{stamina_needed}")

    # Last resort: Full Stamina Potion only when all partial potions are exhausted
    partials_remaining = any(
        (not p.is_full) and p.quantity > 0 for p in potions
    )
    if stamina_needed > 0 and full_potion and full_potion.quantity > 0 and not partials_remaining:
        state.log(f"  No partial potions left — using {full_potion.name} as last resort...")
        try:
            ok = await game.use_stamina_potion(full_potion.inv_id)
        except Exception as e:
            state.log(f"  Error: {e}")
        else:
            if ok:
                state.log(f"  {full_potion.name} used! Stamina fully restored")
                return True

    if any_used:
        return True

    state.log("  No usable potions")
    return False


async def single_attack(
    game: GameClient,
    monster_id: str,
    stamina_label: str,
    state: FarmerState,
    limiter: RateLimiter,
) -> str:
    """Hit once (goal=0 mode). Returns: "ok" | "dead" | "stamina" | "error"."""
    stam = get_stamina_option(stamina_label)

    while state.running:
        await limiter.wait()
        try:
            result = await game.attack(monster_id, stam["skill_id"], stam["cost"])
        except Exception as e:
            state.log(f"    Error: {e}")
            game.record_net_failure()
            return "error"

        game.record_net_success()

        if result.is_success:
            state.stats.stamina_spent += stam["cost"]
            state.stats.damage += result.damage
            if result.is_dead:
                state.stats.killed += 1
            limiter.on_success()
            state.log(f"    Hit for {result.damage:,} damage")
            return "ok"

        if result.is_dead:
            state.log("    Already dead")
            return "dead"

        if result.is_stamina_exhausted:
            next_stam = step_down_stamina(stam["cost"])
            if next_stam:
                stam = next_stam
                state.log(f"    Downgrading to {stam['cost']} Stamina...")
                await asyncio.sleep(1)
                continue
            state.log("    OUT OF STAMINA (0 left)")
            return "stamina"

        state.log(f"    Failed: {result.message}")
        return "error"

    return "error"


async def worker(
    game: GameClient,
    targets: list[TargetConfig],
    state: FarmerState,
    limiter: RateLimiter,
    recheck_priority: bool = False,
) -> None:
    """
    Main farming loop:
    1. Re-fetch each wave to get fresh HP / alive status
    2. Attack eligible monsters by priority
    3. Wait for respawns, repeat

    When `recheck_priority` is True, after a target produces any attacks we
    invalidate the wave cache and restart from priority 1 on the next target
    pick. That way higher-priority mobs that respawned while we were busy on
    a lower one get picked up mid-round instead of only after the respawn
    sleep.
    """
    targets.sort(key=lambda t: t.priority)
    total_attacked = 0
    rounds = 0

    # Launch background reaction farming to top up stamina while we battle
    from veyra.engine.stamina_farmer import reaction_topup_loop
    reaction_task = asyncio.create_task(reaction_topup_loop(game, state))

    try:
        while state.running:
            rounds += 1
            state.stats.rounds = rounds
            any_attacked = False
            wave_cache: dict[int, list[Monster]] = {}
            # Tracks which targets have been visited this round — with
            # recheck_priority, we revisit priority-1 only if it genuinely
            # respawned (fresh list non-empty), otherwise advance linearly.
            skipped_in_round: set[int] = set()

            # If site went down (detected during previous round), wait for recovery
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    state.log, lambda: not state.running
                )
                if not recovered:
                    break

            ti_idx = 0
            site_down_break = False
            while ti_idx < len(targets):
                if not state.running:
                    break
                if ti_idx in skipped_in_round:
                    ti_idx += 1
                    continue

                t = targets[ti_idx]
                ti = ti_idx + 1  # 1-based for display

                # Fetch wave (cached until we choose to invalidate)
                if t.wave not in wave_cache:
                    try:
                        wave_cache[t.wave] = await game.fetch_wave(t.wave)
                        game.record_net_success()
                    except Exception as e:
                        state.log(f"  Fetch wave {t.wave} failed: {e}")
                        game.record_net_failure()
                        if game.is_site_down:
                            site_down_break = True
                            break
                        wave_cache[t.wave] = []

                fresh = [m for m in wave_cache[t.wave] if m.name == t.name]

                # Separate joined vs new monsters
                joined = [m for m in fresh if m.joined]
                new_monsters = [m for m in fresh if not m.joined]

                # Skip new monsters with HP below goal
                if t.damage_goal > 0:
                    before = len(new_monsters)
                    new_monsters = [
                        m for m in new_monsters
                        if m.current_hp == 0 or m.current_hp >= t.damage_goal
                    ]
                    skipped_hp = before - len(new_monsters)
                else:
                    skipped_hp = 0

                # Skip mobs we've already damaged past the goal — nothing more
                # to earn from them until they respawn.
                skipped_done = 0
                if t.damage_goal > 0:
                    before_j = len(joined) + len(new_monsters)
                    joined = [m for m in joined if m.your_dmg < t.damage_goal]
                    new_monsters = [m for m in new_monsters if m.your_dmg < t.damage_goal]
                    skipped_done = before_j - (len(joined) + len(new_monsters))

                # Combine: joined first (continue fighting), then new
                fresh = joined + sorted(new_monsters, key=lambda x: x.current_hp, reverse=True)

                if not fresh:
                    skipped_in_round.add(ti_idx)
                    ti_idx += 1
                    continue

                mode = "hit once" if t.damage_goal <= 0 else f"{t.damage_goal:,} dmg"
                state.log("")
                state.log(f"[{ti}/{len(targets)}] {t.name}  ({len(new_monsters)} new, {len(joined)} joined, {mode})")
                if skipped_hp:
                    state.log(f"  Skipped {skipped_hp} (HP < {t.damage_goal:,})")
                if skipped_done:
                    state.log(f"  Skipped {skipped_done} (already ≥ {t.damage_goal:,} dmg dealt)")

                attacked_this_target = False
                for ii, inst in enumerate(fresh, 1):
                    if not state.running:
                        break

                    tag = " (continuing)" if inst.joined else ""
                    state.log(f"  ({ii}/{len(fresh)}) ID {inst.id}  HP: {inst.current_hp:,}{tag}")

                    try:
                        await game.join_battle(inst.id)
                        await asyncio.sleep(1)
                    except Exception as e:
                        if not inst.joined:
                            state.log(f"    Join failed: {e}")
                            continue
                        # Already joined — continue anyway

                    if t.damage_goal <= 0:
                        r = await single_attack(game, inst.id, t.stamina, state, limiter)
                    else:
                        r = await farm_monster(
                            game, inst.id, t.stamina, t.damage_goal, t.name, state, limiter
                        )

                    if r == "stamina":
                        state.log("")
                        state.log(
                            f"=== STAMINA EXHAUSTED "
                            f"(attacked {total_attacked} monsters in {rounds} rounds) ==="
                        )
                        # Try smart loot to level up and restore stamina
                        from veyra.engine.loot_collector import smart_loot
                        leveled = await smart_loot(
                            game, state,
                            waves=list({t.wave for t in targets}),
                        )
                        if leveled:
                            state.log("Stamina restored! Continuing farming...")
                            break  # break inner monster loop, restart round

                        # Smart loot failed — try stamina potions
                        potion_used = await _try_stamina_potion(game, state, wave=t.wave)
                        if potion_used:
                            state.log("Stamina potion used! Continuing farming...")
                            break
                        else:
                            logger.warning("Farming stopped: stamina exhausted, no recovery available")
                            state.log("No stamina potions available — stopping.")
                            state.stop()
                            return

                    if r in ("done", "ok"):
                        any_attacked = True
                        attacked_this_target = True
                        total_attacked += 1
                        state.stats.monsters_attacked = total_attacked

                # After finishing this target's instances
                if recheck_priority and attacked_this_target:
                    # Re-scan from priority 1 with a fresh wave snapshot —
                    # higher-priority mobs that respawned while we were busy
                    # get picked up immediately.
                    wave_cache.clear()
                    skipped_in_round.clear()
                    skipped_in_round.add(ti_idx)  # current target just drained
                    ti_idx = 0
                else:
                    skipped_in_round.add(ti_idx)
                    ti_idx += 1

            if site_down_break:
                pass  # falls through to outer site-down wait next iteration

            if not state.running:
                break

            if any_attacked:
                state.log("")
                state.log(f"Round {rounds} done ({total_attacked} attacked). Checking for respawns...")
            else:
                state.log(f"No eligible monsters. Waiting {RESPAWN_WAIT}s...")

            # Sleep in 1s ticks for responsive stopping
            for _ in range(RESPAWN_WAIT):
                if not state.running:
                    break
                await asyncio.sleep(1)

        state.log("")
        state.log(f"=== STOPPED  ({total_attacked} monsters in {rounds} rounds) ===")
    except Exception as e:
        logger.error("Farming worker fatal error: %s", e, exc_info=True)
        state.log(f"Fatal: {e}")
    finally:
        reaction_task.cancel()
        try:
            await reaction_task
        except (asyncio.CancelledError, Exception):
            pass
        state.stop()
