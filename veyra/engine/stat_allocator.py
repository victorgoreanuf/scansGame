"""Auto stat allocation — periodically checks and allocates unspent points.

Logic: keep the target stat at clean multiples of 10.
  - If stat ends in e.g. 5 and we have 5 points → allocate 5 to round up to next 10.
  - If stat is already a multiple of 10 and we have 10 points → allocate 10.
"""

import asyncio
from dataclasses import dataclass, field

from veyra.game.client import GameClient

CHECK_INTERVAL = 300  # seconds between checks (5 minutes)


@dataclass
class StatAllocatorState:
    running: bool = False
    target_stat: str = ""  # "attack", "defense", or "stamina"
    total_allocated: int = 0
    current_attack: int = 0
    current_defense: int = 0
    current_stamina: int = 0
    unspent: int = 0
    logs: list[dict] = field(default_factory=list)
    _log_id: int = 0

    def log(self, msg: str) -> None:
        self._log_id += 1
        self.logs.append({"id": self._log_id, "msg": msg})

    def stop(self) -> None:
        self.running = False


def _compute_allocation(current_value: int, unspent: int) -> int:
    """How many points to allocate to round up to the next multiple of 10.

    Returns 0 if not enough points available yet.
    """
    remainder = current_value % 10
    needed = (10 - remainder) if remainder > 0 else 10
    return needed if unspent >= needed else 0


async def stat_allocator_worker(game: GameClient, state: StatAllocatorState) -> None:
    """Periodically check unspent stat points and allocate them."""
    state.log(f"[Stats] Auto-allocate started -> {state.target_stat.upper()}")
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

            current_value = getattr(char_stats, state.target_stat, 0)
            to_allocate = _compute_allocation(current_value, char_stats.unspent)

            if to_allocate > 0:
                state.log(
                    f"[Stats] {state.target_stat.upper()} is {current_value}, "
                    f"{char_stats.unspent} unspent -> allocating {to_allocate}"
                )
                result = await game.allocate_stat(state.target_stat, to_allocate)
                game.record_net_success()

                status = result.get("status", "")
                if status == "success" or "success" in str(result).lower():
                    state.total_allocated += to_allocate
                    new_val = current_value + to_allocate
                    setattr(char_stats, state.target_stat, new_val)
                    # Update displayed value
                    if state.target_stat == "attack":
                        state.current_attack = new_val
                    elif state.target_stat == "defense":
                        state.current_defense = new_val
                    else:
                        state.current_stamina = new_val
                    state.unspent = char_stats.unspent - to_allocate
                    state.log(
                        f"[Stats] +{to_allocate} -> {state.target_stat.upper()} = {new_val} "
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
