"""Quest board automation — accepts quests, executes them, falls back to wave farming."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from veyra.game.client import GameClient
from veyra.game.endpoints import WAVE_MAP, get_stamina_option
from veyra.game.types import (
    ActiveQuest,
    Quest,
    QuestObjective,
    QuestStatus,
    QuestType,
    Monster,
    TargetConfig,
)
from veyra.engine.rate_limiter import RateLimiter

logger = logging.getLogger("veyra.quest")

RESPAWN_WAIT = 30
QUEST_RECHECK_INTERVAL = 300  # 5 minutes — recheck quest board during fallback
MP_RECHECK_INTERVAL = 1800    # 30 minutes — recheck MP for skill quests
MAX_IDLE_ROUNDS = 3           # rounds with nothing to do before falling back
MAX_NO_PROGRESS_ROUNDS = 15   # rounds with loots but no quest progress before blocked


@dataclass
class QuestState:
    running: bool = False
    quests_completed: int = 0
    current_quest: str = ""
    current_progress: str = ""
    farming_fallback: bool = False
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


# ── Quest evaluation ─────────────────────────────────────────────────────────


async def _find_monster_wave(
    monster_name: str, game: GameClient
) -> int | None:
    """Scan waves 1-4 to find which wave has the target monster."""
    for wave_num in WAVE_MAP:
        try:
            monsters = await game.fetch_wave(wave_num)
            for m in monsters:
                if m.name.lower() == monster_name.lower():
                    return wave_num
        except Exception:
            continue
    return None


def _evaluate_quest(
    quest: Quest,
    has_class_skills: bool,
    loot_db,
) -> tuple[int, dict]:
    """Evaluate a quest's priority and build an execution plan.

    Returns (priority_score, plan_dict). Score 0 = infeasible.
    """
    obj = quest.objective

    if obj.quest_type == QuestType.GATHER:
        results = loot_db.find_item(obj.target_name)
        if results:
            best = max(results, key=lambda r: _parse_drop_pct(r["item"].get("drop_rate", "0%")))
            return 100, {
                "type": "gather",
                "monster_name": best["monster_name"],
                "item_name": obj.target_name,
                "drop_rate": best["item"].get("drop_rate", "?"),
                "dmg_required": best["item"].get("dmg_required", 0),
                "count": obj.target_count,
            }
        return 0, {}

    if obj.quest_type == QuestType.KILL:
        return 50, {
            "type": "kill",
            "monster_name": obj.target_name,
            "min_damage": obj.min_damage,
            "count": obj.target_count,
        }

    if obj.quest_type == QuestType.SKILL:
        if not has_class_skills:
            return 0, {}
        return 10, {
            "type": "skill",
            "count": obj.target_count,
        }

    return 0, {}


def _parse_drop_pct(rate_str: str) -> float:
    """Parse '6%' -> 6.0, '90%' -> 90.0."""
    try:
        return float(rate_str.replace("%", "").strip())
    except (ValueError, AttributeError):
        return 0.0


# ── Loot existing corpses ─────────────────────────────────────────────────────


async def _loot_existing_corpses(
    game: GameClient,
    state: QuestState,
    monster_name: str | None,
    min_damage: int,
    max_loots: int,
) -> int:
    """Check all waves for dead monsters we can loot right now.

    For kill quests: matches monster_name and checks our damage >= min_damage.
    For gather quests: matches monster_name (any damage counts for loot).

    Returns number of corpses looted.
    """
    looted = 0

    for wave_num in WAVE_MAP:
        if looted >= max_loots:
            break
        try:
            dead = await game.fetch_dead_monsters(wave_num)
        except Exception:
            continue

        for d in dead:
            if looted >= max_loots:
                break
            # Filter by monster name if specified
            if monster_name and d.name.lower() != monster_name.lower():
                continue
            # For kill quests, check minimum damage dealt
            if min_damage > 0 and d.your_dmg < min_damage:
                continue
            # Must have some damage to loot
            if d.your_dmg <= 0:
                continue

            state.log(f"  Found lootable {d.name} (dmg={d.your_dmg:,}) — looting...")
            try:
                result = await game.loot_monster(d.id)
                state.log(f"    Loot: {result.get('message', result.get('status', 'ok'))}")
                looted += 1
            except Exception as e:
                state.log(f"    Loot error: {e}")
            await asyncio.sleep(1)

    return looted


# ── Quest execution ──────────────────────────────────────────────────────────


async def _execute_kill_quest(
    game: GameClient,
    state: QuestState,
    plan: dict,
    objective: QuestObjective,
    limiter: RateLimiter,
) -> str:
    """Execute a kill quest: deal min_damage to target monsters, then loot
    their corpses once they die. Only dead monsters can be looted.

    Strategy:
    1. Loot any existing dead corpses with our dmg >= min_damage
    2. Deal min_damage to alive targets (so we qualify for loot when they die)
    3. Check for new corpses and loot them
    4. Re-check quest board for progress
    5. If no progress after many rounds — return blocked

    Returns: "done" | "blocked" | "stamina" | "error"
    """
    from veyra.engine.wave_farmer import farm_monster, _try_stamina_potion

    monster_name = plan["monster_name"]
    min_damage = plan["min_damage"]
    idle_rounds = 0
    last_known_progress = 0

    # First: check for existing corpses we can loot immediately
    state.log(f"  Checking for lootable {monster_name} corpses...")
    free_loots = await _loot_existing_corpses(
        game, state, monster_name, min_damage, objective.target_count,
    )
    if free_loots > 0:
        state.log(f"  Free loots: {free_loots}")

    # Check initial quest progress
    try:
        active = await game.fetch_active_quest()
        if active and active.completed:
            return "done"
        if active:
            last_known_progress = active.progress
            state.current_progress = f"{active.progress}/{objective.target_count} looted"
            state.log(f"  Quest board: {active.progress}/{objective.target_count}")
            if active.progress >= objective.target_count:
                return "done"
    except Exception:
        pass

    while state.running:
        # Nothing to do for several rounds — fall back to wave farming
        if idle_rounds >= MAX_IDLE_ROUNDS:
            state.log(f"  Nothing to do for {idle_rounds} rounds — blocked")
            return "blocked"

        # Find which wave has the target
        wave_num = await _find_monster_wave(monster_name, game)
        if wave_num is None:
            state.log(f"  {monster_name} not found on any wave")
            return "blocked"

        # Phase A: Deal damage to alive monsters we haven't joined yet
        monsters = await game.fetch_wave(wave_num)
        targets = [m for m in monsters if m.name.lower() == monster_name.lower() and not m.joined]

        if min_damage > 0:
            targets = [m for m in targets if m.current_hp == 0 or m.current_hp >= min_damage]

        attacked = 0
        if targets:
            for monster in targets:
                if not state.running:
                    break

                state.log(f"  Attacking {monster.name} (id={monster.id}, HP={monster.current_hp:,})")

                try:
                    await game.join_battle(monster.id)
                    await asyncio.sleep(1)
                except Exception as e:
                    state.log(f"    Join failed: {e}")
                    continue

                from veyra.engine.wave_farmer import FarmerState
                temp_state = FarmerState()
                temp_state.running = True
                temp_state.log = state.log  # type: ignore[assignment]

                result = await farm_monster(
                    game, monster.id, "10 Stamina",
                    min_damage or 1, monster.name, temp_state, limiter,
                )

                if result == "stamina":
                    potion_ok = await _try_stamina_potion(game, temp_state, wave_num)
                    if potion_ok:
                        state.log("  Stamina restored via potion, retrying...")
                        result = await farm_monster(
                            game, monster.id, "10 Stamina",
                            min_damage or 1, monster.name, temp_state, limiter,
                        )
                    if result == "stamina":
                        return "stamina"

                attacked += 1
        else:
            state.log(f"  No eligible {monster_name} to attack")

        # Phase B: Loot dead corpses of this monster
        state.log(f"  Checking for dead {monster_name} to loot...")
        corpse_loots = await _loot_existing_corpses(
            game, state, monster_name, min_damage, objective.target_count,
        )
        if corpse_loots > 0:
            state.log(f"  Looted {corpse_loots} corpses")

        # Phase C: Check quest board for actual progress
        try:
            active = await game.fetch_active_quest()
            if active:
                if active.completed:
                    state.log("  Quest completed!")
                    return "done"
                current = active.progress
                if current > last_known_progress:
                    idle_rounds = 0  # progress! reset idle
                    last_known_progress = current
                state.current_progress = f"{current}/{objective.target_count} looted"
                state.log(f"  Quest board: {current}/{objective.target_count}")
                if current >= objective.target_count:
                    return "done"
        except Exception as e:
            state.log(f"  Failed to check quest board: {e}")

        # Track idle: nothing attacked AND nothing looted = idle
        if attacked == 0 and corpse_loots == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        # Wait for monsters to respawn / die
        state.log(f"  Waiting {RESPAWN_WAIT}s for respawns...")
        await _sleep(state, RESPAWN_WAIT)

    return "error"


async def _execute_gather_quest(
    game: GameClient,
    state: QuestState,
    plan: dict,
    objective: QuestObjective,
    limiter: RateLimiter,
) -> str:
    """Execute a gather quest: deal dmg_required to target monsters, then loot
    their corpses once they die. Only dead monsters can be looted.

    Strategy:
    1. Loot any existing dead corpses of the target monster
    2. Deal dmg_required to alive targets (so we qualify for loot when they die)
    3. Check for new corpses and loot them
    4. Re-check quest board for progress
    5. If no progress after many rounds — return blocked

    Returns: "done" | "blocked" | "stamina" | "error"
    """
    from veyra.engine.wave_farmer import farm_monster, _try_stamina_potion

    monster_name = plan["monster_name"]
    dmg_required = plan.get("dmg_required", 0) or 1
    idle_rounds = 0
    no_progress_rounds = 0
    last_known_progress = 0

    # First: loot existing dead corpses of this monster type
    state.log(f"  Checking for lootable {monster_name} corpses...")
    free_loots = await _loot_existing_corpses(
        game, state, monster_name, 0, objective.target_count * 3,
    )
    if free_loots > 0:
        state.log(f"  Looted {free_loots} existing corpses")

    # Check initial quest progress
    try:
        active = await game.fetch_active_quest()
        if active and active.completed:
            return "done"
        if active:
            last_known_progress = active.progress
            state.current_progress = f"{active.progress}/{objective.target_count} gathered"
            state.log(f"  Quest board: {active.progress}/{objective.target_count}")
            if active.progress >= objective.target_count:
                return "done"
    except Exception:
        pass

    while state.running:
        # Nothing to do for several rounds — fall back to wave farming
        if idle_rounds >= MAX_IDLE_ROUNDS:
            state.log(f"  Nothing to do for {idle_rounds} rounds — blocked")
            return "blocked"

        # Actively looting but no quest progress — drop rate too low
        if no_progress_rounds >= MAX_NO_PROGRESS_ROUNDS:
            state.log(f"  {no_progress_rounds} rounds with loots but no quest progress — blocked")
            return "blocked"

        # Find target monster wave
        wave_num = await _find_monster_wave(monster_name, game)
        if wave_num is None:
            state.log(f"  {monster_name} not found on any wave")
            return "blocked"

        # Phase A: Deal damage to alive monsters we haven't joined yet
        monsters = await game.fetch_wave(wave_num)
        targets = [m for m in monsters if m.name.lower() == monster_name.lower() and not m.joined]

        attacked = 0
        if targets:
            for monster in targets:
                if not state.running:
                    break

                state.log(f"  Attacking {monster.name} (id={monster.id}, HP={monster.current_hp:,})")

                try:
                    await game.join_battle(monster.id)
                    await asyncio.sleep(1)
                except Exception as e:
                    state.log(f"    Join failed: {e}")
                    continue

                from veyra.engine.wave_farmer import FarmerState
                temp_state = FarmerState()
                temp_state.running = True
                temp_state.log = state.log  # type: ignore[assignment]

                result = await farm_monster(
                    game, monster.id, "10 Stamina",
                    dmg_required, monster.name, temp_state, limiter,
                )

                if result == "stamina":
                    potion_ok = await _try_stamina_potion(game, temp_state, wave_num)
                    if potion_ok:
                        result = await farm_monster(
                            game, monster.id, "10 Stamina",
                            dmg_required, monster.name, temp_state, limiter,
                        )
                    if result == "stamina":
                        return "stamina"

                attacked += 1
        else:
            state.log(f"  No new {monster_name} to attack")

        # Phase B: Loot any dead corpses of the target monster
        state.log(f"  Checking for dead {monster_name} to loot...")
        corpse_loots = await _loot_existing_corpses(
            game, state, monster_name, 0, 20,
        )
        if corpse_loots > 0:
            state.log(f"  Looted {corpse_loots} corpses")

        # Phase C: Check quest board for actual progress
        try:
            active = await game.fetch_active_quest()
            if active:
                if active.completed:
                    state.log("  Quest completed!")
                    return "done"
                current = active.progress
                if current > last_known_progress:
                    idle_rounds = 0
                    no_progress_rounds = 0
                    last_known_progress = current
                elif corpse_loots > 0:
                    # We looted but no item dropped — track separately
                    no_progress_rounds += 1
                state.current_progress = f"{current}/{objective.target_count} gathered"
                state.log(f"  Quest board: {current}/{objective.target_count}")
                if current >= objective.target_count:
                    return "done"
        except Exception as e:
            state.log(f"  Failed to check quest board: {e}")

        # Track idle: nothing attacked AND nothing looted = idle
        if attacked == 0 and corpse_loots == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        # Wait for monsters to respawn / die
        state.log(f"  Waiting {RESPAWN_WAIT}s for respawns...")
        await _sleep(state, RESPAWN_WAIT)

    return "error"


async def _execute_skill_quest(
    game: GameClient,
    state: QuestState,
    plan: dict,
    objective: QuestObjective,
    limiter: RateLimiter,
    class_skills: list[dict],
) -> str:
    """Execute a skill quest: use cheapest class skill against any monster.

    Dumps all MP then returns "blocked" so the runner can fall back to wave farming
    while MP regens. Called repeatedly until quest progress reaches target.

    Returns: "done" | "blocked" | "stamina" | "error"
    """
    if not class_skills:
        return "blocked"

    # Pick cheapest MP skill
    cheapest = min(class_skills, key=lambda s: s["mp_cost"])
    skill_id = cheapest["id"]
    mp_cost = cheapest["mp_cost"]
    state.log(f"  Using {cheapest['name']} ({mp_cost} MP)")

    # We need to be joined to a battle to use skills
    # Find any monster on wave 1
    try:
        monsters = await game.fetch_wave(1)
    except Exception:
        return "error"

    if not monsters:
        return "blocked"

    target = monsters[0]
    try:
        await game.join_battle(target.id)
        await asyncio.sleep(1)
    except Exception:
        return "error"

    # Dump all MP
    uses = 0
    errors = 0
    while state.running and errors < 5:
        await limiter.wait()
        try:
            result = await game.use_class_skill(target.id, skill_id)
            game.record_net_success()
        except Exception as e:
            state.log(f"    Skill error: {e}")
            game.record_net_failure()
            errors += 1
            continue

        if result.is_success:
            uses += 1
            errors = 0
            limiter.on_success()
            state.log(f"    Skill used ({uses} this session)")
        elif result.is_dead:
            # Monster died, find another
            state.log("    Monster died, finding another...")
            try:
                monsters = await game.fetch_wave(1)
                alive = [m for m in monsters if not m.joined]
                if not alive:
                    alive = monsters
                if not alive:
                    state.log("    No monsters available")
                    break
                target = alive[0]
                await game.join_battle(target.id)
                await asyncio.sleep(1)
            except Exception:
                break
        elif result.is_stamina_exhausted:
            # Class skills cost 1 stamina — if out, need recovery
            return "stamina"
        else:
            # Likely out of MP
            msg = result.message.lower() if result.message else ""
            if "mana" in msg or "mp" in msg or "not enough" in msg:
                state.log(f"    Out of MP after {uses} uses — need to wait for regen")
                break
            errors += 1
            state.log(f"    Unexpected: {result.message}")

    # Check quest progress
    try:
        active = await game.fetch_active_quest()
        if active and active.completed:
            return "done"
        if active:
            state.current_progress = f"{active.progress}/{objective.target_count} skills"
            state.log(f"  Quest board: {active.progress}/{objective.target_count}")
            if active.progress >= objective.target_count:
                return "done"
    except Exception:
        pass

    # MP exhausted — return blocked so we fall back to wave farming
    return "blocked"


# ── Fallback wave farming ────────────────────────────────────────────────────


async def _run_fallback_farming(
    game: GameClient,
    state: QuestState,
    targets: list[TargetConfig],
    limiter: RateLimiter,
    duration: int = QUEST_RECHECK_INTERVAL,
) -> None:
    """Run wave farming for a limited duration, then return for quest re-check."""
    from veyra.engine.wave_farmer import farm_monster, single_attack, _try_stamina_potion

    state.farming_fallback = True
    state.log("")
    state.log(f"=== FALLBACK: Wave farming for ~{duration}s ===")

    if not targets:
        state.log("  No farming targets configured, waiting...")
        await _sleep(state, duration)
        state.farming_fallback = False
        return

    sorted_targets = sorted(targets, key=lambda t: t.priority)
    start = time.time()

    while state.running and (time.time() - start) < duration:
        for t in sorted_targets:
            if not state.running or (time.time() - start) >= duration:
                break

            try:
                monsters = await game.fetch_wave(t.wave)
                game.record_net_success()
            except Exception as e:
                state.log(f"  Wave {t.wave} fetch failed: {e}")
                game.record_net_failure()
                continue

            fresh = [m for m in monsters if m.name == t.name and not m.joined]
            if t.damage_goal > 0:
                fresh = [m for m in fresh if m.current_hp == 0 or m.current_hp >= t.damage_goal]
            fresh.sort(key=lambda x: x.current_hp, reverse=True)

            if not fresh:
                continue

            from veyra.engine.wave_farmer import FarmerState
            temp_state = FarmerState()
            temp_state.running = True
            temp_state.log = state.log  # type: ignore[assignment]

            for inst in fresh:
                if not state.running or (time.time() - start) >= duration:
                    break

                try:
                    await game.join_battle(inst.id)
                    await asyncio.sleep(1)
                except Exception:
                    continue

                if t.damage_goal <= 0:
                    r = await single_attack(game, inst.id, t.stamina, temp_state, limiter)
                else:
                    r = await farm_monster(
                        game, inst.id, t.stamina, t.damage_goal, t.name, temp_state, limiter,
                    )

                if r == "stamina":
                    potion_ok = await _try_stamina_potion(game, temp_state, t.wave)
                    if not potion_ok:
                        state.log("  Fallback: out of stamina, waiting...")
                        await _sleep(state, RESPAWN_WAIT)

        # Brief wait between farming rounds
        await _sleep(state, RESPAWN_WAIT)

    state.farming_fallback = False
    state.log("=== FALLBACK: Returning to quest check ===")


# ── Main quest worker ────────────────────────────────────────────────────────


async def quest_worker(
    game: GameClient,
    state: QuestState,
    loot_db,
    targets: list[TargetConfig],
    limiter: RateLimiter,
) -> None:
    """Main quest automation loop.

    1. Check for active quest — execute or finish it
    2. Pick best available quest and accept it
    3. Execute quest (kill/gather/skill)
    4. If blocked — fall back to wave farming, recheck periodically
    5. Loop
    """
    state.log("=== Quest Runner Started ===")

    # Detect class skills once at startup
    class_skills: list[dict] = []
    try:
        monsters = await game.fetch_wave(1)
        if monsters:
            await game.join_battle(monsters[0].id)
            await asyncio.sleep(1)
            class_skills = await game.fetch_class_skills(monsters[0].id)
            if class_skills:
                names = ", ".join(s["name"] for s in class_skills)
                state.log(f"Class skills detected: {names}")
            else:
                state.log("No class skills — skill quests will be skipped")
    except Exception as e:
        state.log(f"Failed to detect class skills: {e}")

    try:
        while state.running:
            # Site health check
            if game.is_site_down:
                recovered = await game.wait_for_site_up(state.log, lambda: not state.running)
                if not recovered:
                    break

            state.log("")
            state.log("--- Checking quest board ---")

            # 1. Fetch quest board
            try:
                quests = await game.fetch_quest_board()
                active = await game.fetch_active_quest()
                game.record_net_success()
            except Exception as e:
                state.log(f"Failed to fetch quest board: {e}")
                game.record_net_failure()
                await _sleep(state, 60)
                continue

            # 2. Handle active quest
            if active:
                state.current_quest = active.quest.title
                state.log(f"Active quest: {active.quest.title}")
                state.log(f"  Progress: {active.progress}/{active.target_count}")

                if active.completed:
                    # Turn in the quest
                    state.log("  Quest complete! Turning in...")
                    try:
                        result = await game.finish_quest(active.quest.quest_id)
                        msg = result.get("message", result.get("status", "ok"))
                        state.log(f"  Turn in: {msg}")
                        state.quests_completed += 1
                        state.current_quest = ""
                        state.current_progress = ""
                    except Exception as e:
                        state.log(f"  Turn in failed: {e}")
                        await _sleep(state, 30)
                    continue  # Loop back to pick next quest

                # Execute the active quest
                result = await _execute_active_quest(
                    game, state, active, limiter, class_skills, loot_db,
                )

                if result == "done":
                    # Re-check board — quest should be completable now
                    continue
                elif result == "blocked":
                    state.log("Quest is blocked — falling back to wave farming")
                    await _run_fallback_farming(game, state, targets, limiter)
                    continue
                elif result == "stamina":
                    state.log("Out of stamina during quest")
                    from veyra.engine.loot_collector import smart_loot
                    from veyra.engine.wave_farmer import FarmerState, _try_stamina_potion
                    temp_state = FarmerState()
                    temp_state.running = True
                    temp_state.log = state.log  # type: ignore[assignment]
                    leveled = await smart_loot(game, temp_state, waves=[1, 2])
                    if not leveled:
                        potion_ok = await _try_stamina_potion(game, temp_state)
                        if not potion_ok:
                            state.log("No stamina recovery — fallback farming")
                            await _run_fallback_farming(game, state, targets, limiter, 120)
                    continue
                else:
                    await _sleep(state, 30)
                    continue

            # 3. No active quest — pick one
            available = [q for q in quests if q.status == QuestStatus.AVAILABLE]
            state.log(f"Available quests: {len(available)}")

            if not available:
                # Check cooldowns
                cooldown_quests = [q for q in quests if q.status == QuestStatus.COOLDOWN]
                if cooldown_quests:
                    soonest = min(q.cooldown_ts for q in cooldown_quests)
                    wait = max(0, soonest - int(time.time()))
                    state.log(f"All on cooldown. Soonest available in {wait}s")
                state.log("No quests available — fallback to wave farming")
                await _run_fallback_farming(game, state, targets, limiter)
                continue

            # Evaluate and rank
            ranked: list[tuple[int, Quest, dict]] = []
            for q in available:
                score, plan = _evaluate_quest(q, bool(class_skills), loot_db)
                if score > 0:
                    ranked.append((score, q, plan))
                else:
                    state.log(f"  Skipping '{q.title}' — infeasible")

            ranked.sort(key=lambda x: x[0], reverse=True)

            if not ranked:
                state.log("No feasible quests — fallback to wave farming")
                await _run_fallback_farming(game, state, targets, limiter)
                continue

            # Accept the best quest
            _, best_quest, best_plan = ranked[0]
            state.log(f"Accepting: {best_quest.title} (id={best_quest.quest_id})")
            state.log(f"  Type: {best_quest.objective.quest_type.value}, Plan: {best_plan}")

            try:
                accept_result = await game.accept_quest(best_quest.quest_id)
                msg = accept_result.get("message", accept_result.get("status", "ok"))
                state.log(f"  Accept: {msg}")
                if accept_result.get("status") != "ok":
                    state.log("  Accept failed, trying next...")
                    await _sleep(state, 10)
                    continue
            except Exception as e:
                state.log(f"  Accept error: {e}")
                await _sleep(state, 10)
                continue

            state.current_quest = best_quest.title
            state.current_progress = "0/?"
            # Loop back — next iteration will find it as active quest

    except Exception as e:
        logger.error("Quest runner fatal error: %s", e, exc_info=True)
        state.log(f"Quest runner fatal: {e}")
    finally:
        state.log("")
        state.log(f"=== Quest Runner Stopped ({state.quests_completed} completed) ===")
        state.farming_fallback = False
        state.stop()


async def _execute_active_quest(
    game: GameClient,
    state: QuestState,
    active: ActiveQuest,
    limiter: RateLimiter,
    class_skills: list[dict],
    loot_db,
) -> str:
    """Route to the correct execution function based on quest type."""
    obj = active.quest.objective

    if obj.quest_type == QuestType.KILL:
        # Build plan from objective
        plan = {
            "type": "kill",
            "monster_name": obj.target_name,
            "min_damage": obj.min_damage,
            "count": obj.target_count,
        }
        return await _execute_kill_quest(game, state, plan, obj, limiter)

    if obj.quest_type == QuestType.GATHER:
        # Look up monster from loot_db
        results = loot_db.find_item(obj.target_name)
        if not results:
            state.log(f"  No monster found that drops '{obj.target_name}'")
            return "blocked"
        best = max(results, key=lambda r: _parse_drop_pct(r["item"].get("drop_rate", "0%")))
        plan = {
            "type": "gather",
            "monster_name": best["monster_name"],
            "item_name": obj.target_name,
            "drop_rate": best["item"].get("drop_rate", "?"),
            "dmg_required": best["item"].get("dmg_required", 0),
            "count": obj.target_count,
        }
        return await _execute_gather_quest(game, state, plan, obj, limiter)

    if obj.quest_type == QuestType.SKILL:
        if not class_skills:
            return "blocked"
        plan = {"type": "skill", "count": obj.target_count}
        return await _execute_skill_quest(game, state, plan, obj, limiter, class_skills)

    state.log(f"  Unknown quest type: {obj.quest_type}")
    return "blocked"


async def _sleep(state: QuestState, seconds: int) -> None:
    """Sleep in 1s increments for responsive stopping."""
    for _ in range(seconds):
        if not state.running:
            break
        await asyncio.sleep(1)
