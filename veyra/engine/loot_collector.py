"""Smart loot collector — loots just enough corpses to level up and restore stamina."""

import asyncio
import logging

from veyra.game.client import GameClient
from veyra.game.types import DeadMonster, PlayerStats
from veyra.engine.wave_farmer import FarmerState

logger = logging.getLogger("veyra.loot")


async def smart_loot(
    game: GameClient,
    state: FarmerState,
    waves: list[int] | None = None,
    exp_per_dmg: float = 0.0,
) -> bool:
    """
    Smart looting strategy:
    1. Check current EXP and EXP needed to level up
    2. Gather all dead monsters we can loot across waves
    3. Calculate EXP each corpse would give (damage * exp_per_dmg)
    4. Loot just enough to level up (restoring stamina)
    5. Save remaining corpses for future level-ups

    Returns True if we leveled up (stamina restored), False otherwise.
    """
    if waves is None:
        waves = [1, 2]

    # 1. Get current player stats
    state.log("")
    state.log("=== SMART LOOT: Checking EXP status ===")
    try:
        player = await game.fetch_player_stats(waves[0])
    except Exception as e:
        state.log(f"  Failed to fetch player stats: {e}")
        return False

    state.log(f"  Level {player.level}  |  EXP {player.exp_current:,} / {player.exp_max:,}")
    state.log(f"  Need {player.exp_needed:,} EXP to level up")
    state.log(f"  Stamina {player.stamina_current} / {player.stamina_max}")

    if player.exp_needed <= 0:
        state.log("  Already at max EXP — no loot needed")
        return False

    # 2. Get EXP/DMG ratio if not provided
    if exp_per_dmg <= 0:
        state.log("  Fetching EXP/DMG ratio...")
        exp_per_dmg = await _discover_exp_per_dmg(game, waves, state)
        if exp_per_dmg <= 0:
            state.log("  Could not determine EXP/DMG ratio — cannot calculate loot")
            return False

    state.log(f"  EXP/DMG ratio: {exp_per_dmg}")

    # 3. Gather all dead monsters across waves
    all_dead: list[DeadMonster] = []
    for wn in waves:
        try:
            dead = await game.fetch_dead_monsters(wn)
            for d in dead:
                d.exp_per_dmg = exp_per_dmg
            all_dead.extend(dead)
            state.log(f"  Wave {wn}: {len(dead)} lootable corpses")
        except Exception as e:
            state.log(f"  Wave {wn} fetch failed: {e}")

    if not all_dead:
        state.log("  No corpses to loot!")
        return False

    # Filter out corpses with no damage (we didn't contribute)
    lootable = [d for d in all_dead if d.your_dmg > 0]
    if not lootable:
        state.log("  No corpses with our damage to loot!")
        return False

    # 4. Calculate total available EXP
    total_available_exp = sum(d.estimated_exp for d in lootable)
    state.log(f"  {len(lootable)} corpses with our damage")
    state.log(f"  Total available EXP: {total_available_exp:,.0f}")

    if total_available_exp < player.exp_needed:
        state.log(f"  Not enough EXP to level up ({total_available_exp:,.0f} < {player.exp_needed:,})")
        state.log("  Saving corpses for later")
        return False

    # 5. Sort by EXP (highest first) and pick just enough to level up
    lootable.sort(key=lambda d: d.estimated_exp, reverse=True)

    to_loot: list[DeadMonster] = []
    exp_accumulated = 0.0

    for d in lootable:
        if exp_accumulated >= player.exp_needed:
            break
        to_loot.append(d)
        exp_accumulated += d.estimated_exp

    remaining = len(lootable) - len(to_loot)
    state.log(f"  Looting {len(to_loot)} corpses for ~{exp_accumulated:,.0f} EXP (saving {remaining})")

    # 6. Loot them
    looted_count = 0
    for d in to_loot:
        if not state.running:
            break
        try:
            result = await game.loot_monster(d.id)
            looted_count += 1
            state.stats.looted += 1
            state.stats.exp_gained += d.estimated_exp
            state.log(f"    Looted {d.name} (id={d.id}) — ~{d.estimated_exp:,.0f} EXP")
            await asyncio.sleep(1)
        except Exception as e:
            state.log(f"    Loot failed for {d.name}: {e}")

    state.log(f"  Looted {looted_count}/{len(to_loot)} corpses")

    # 7. Verify level up by re-checking stats
    try:
        new_stats = await game.fetch_player_stats(waves[0])
        if new_stats.level > player.level:
            state.log(f"  LEVEL UP! {player.level} -> {new_stats.level}")
            state.log(f"  Stamina restored: {new_stats.stamina_current} / {new_stats.stamina_max}")
            return True
        else:
            state.log(f"  EXP now: {new_stats.exp_current:,} / {new_stats.exp_max:,}")
            if new_stats.exp_current >= new_stats.exp_max:
                state.log("  At max EXP — level up should trigger on next action")
                return True
    except Exception:
        pass

    return looted_count > 0


async def _discover_exp_per_dmg(
    game: GameClient,
    waves: list[int],
    state: FarmerState,
) -> float:
    """Try to discover EXP/DMG ratio by checking a battle page."""
    for wn in waves:
        try:
            dead = await game.fetch_dead_monsters(wn)
            if dead:
                ratio = await game.fetch_exp_per_dmg(dead[0].id)
                if ratio > 0:
                    return ratio
        except Exception:
            continue

    # Try alive monsters too
    for wn in waves:
        try:
            monsters = await game.fetch_wave(wn)
            if monsters:
                ratio = await game.fetch_exp_per_dmg(monsters[0].id)
                if ratio > 0:
                    return ratio
        except Exception:
            continue

    return 0.0
