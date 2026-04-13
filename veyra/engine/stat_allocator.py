"""Auto stat allocation with configurable priority goals.

Accepts a list of (stat, target_value) priorities and a default stat.
Allocates to the first stat that hasn't reached its target, then falls
back to the default stat indefinitely. Rounds allocations to multiples of 10.
"""

import asyncio
from dataclasses import dataclass, field

from veyra.game.client import GameClient

CHECK_INTERVAL = 300  # seconds between checks (5 minutes)


@dataclass
class StatGoal:
    """A single stat priority: allocate to `stat` until it reaches `target`."""
    stat: str       # "attack", "defense", or "stamina"
    target: int     # desired value (e.g. 250)


@dataclass
class StatAllocatorState:
    running: bool = False
    goals: list[StatGoal] = field(default_factory=list)
    default_stat: str = "stamina"
    total_allocated: int = 0
    current_attack: int = 0
    current_defense: int = 0
    current_stamina: int = 0
    unspent: int = 0
    active_goal_index: int = 0  # which goal we're currently working on
    logs: list[dict] = field(default_factory=list)
    _log_id: int = 0

    def log(self, msg: str) -> None:
        self._log_id += 1
        self.logs.append({"id": self._log_id, "msg": msg})

    def stop(self) -> None:
        self.running = False


def _compute_allocation(current_value: int, unspent: int, cap: int = 0) -> int:
    """How many points to allocate, rounding up to the next multiple of 10.

    If cap > 0, don't allocate more than needed to reach cap.
    Returns 0 if not enough points available yet.
    """
    remainder = current_value % 10
    needed = (10 - remainder) if remainder > 0 else 10

    # Don't overshoot the target
    if cap > 0:
        room = cap - current_value
        if room <= 0:
            return 0
        needed = min(needed, room)

    return needed if unspent >= needed else 0


def _get_current(state: StatAllocatorState, stat: str) -> int:
    if stat == "attack":
        return state.current_attack
    elif stat == "defense":
        return state.current_defense
    else:
        return state.current_stamina


def _find_active_goal(state: StatAllocatorState) -> tuple[str, int]:
    """Find the stat to allocate to and its cap (0 = no cap).

    Returns (stat_name, cap). Walks the priority list to find the first
    goal not yet reached, otherwise falls back to default_stat.
    """
    for i, goal in enumerate(state.goals):
        current = _get_current(state, goal.stat)
        if current < goal.target:
            state.active_goal_index = i
            return goal.stat, goal.target
    # All goals met — use default
    state.active_goal_index = len(state.goals)
    return state.default_stat, 0


async def stat_allocator_worker(game: GameClient, state: StatAllocatorState) -> None:
    """Periodically check unspent stat points and allocate them."""
    if state.goals:
        goals_str = " -> ".join(
            f"{g.stat.upper()} to {g.target}" for g in state.goals
        )
        state.log(f"[Stats] Priority: {goals_str} -> then {state.default_stat.upper()}")
    else:
        state.log(f"[Stats] All points -> {state.default_stat.upper()}")
    state.log(f"[Stats] Checking every {CHECK_INTERVAL // 60}m, rounding to multiples of 10")

    while state.running:
        # Wait for site recovery if needed
        if game.is_site_down:
            recovered = await game.wait_for_site_up(
                lambda m: state.log(f"[Stats] {m}") if m else None,
                lambda: not state.running,
            )
            if not recovered:
                break
            continue

        try:
            char_stats = await game.fetch_character_stats()
            game.record_net_success()

            # Update state for UI display
            state.current_attack = char_stats.attack
            state.current_defense = char_stats.defense
            state.current_stamina = char_stats.stamina
            state.unspent = char_stats.unspent

            # Find which stat to allocate to
            target_stat, cap = _find_active_goal(state)
            current_value = getattr(char_stats, target_stat, 0)
            to_allocate = _compute_allocation(current_value, char_stats.unspent, cap)

            if to_allocate > 0:
                label = target_stat.upper()
                if cap > 0:
                    state.log(
                        f"[Stats] {label} is {current_value}/{cap}, "
                        f"{char_stats.unspent} unspent -> allocating {to_allocate}"
                    )
                else:
                    state.log(
                        f"[Stats] {label} is {current_value} (default), "
                        f"{char_stats.unspent} unspent -> allocating {to_allocate}"
                    )

                result = await game.allocate_stat(target_stat, to_allocate)
                game.record_net_success()

                status = result.get("status", "")
                if status == "success" or "success" in str(result).lower():
                    state.total_allocated += to_allocate
                    new_val = current_value + to_allocate
                    # Update displayed value
                    if target_stat == "attack":
                        state.current_attack = new_val
                    elif target_stat == "defense":
                        state.current_defense = new_val
                    else:
                        state.current_stamina = new_val
                    state.unspent = char_stats.unspent - to_allocate
                    state.log(
                        f"[Stats] +{to_allocate} -> {target_stat.upper()} = {new_val} "
                        f"(total allocated: {state.total_allocated})"
                    )
                else:
                    msg = result.get("message", str(result)[:200])
                    state.log(f"[Stats] Allocation response: {msg}")

        except Exception as e:
            game.record_net_failure()
            state.log(f"[Stats] Error: {e}")

        # Sleep in 1s ticks for responsive stopping
        for _ in range(CHECK_INTERVAL):
            if not state.running:
                break
            await asyncio.sleep(1)

    state.log("[Stats] Auto-allocate stopped")
