"""Guild Dungeon Army worker — tap-and-run leech for Polyhedral Crucible army rooms."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from veyra.game.client import GameClient
from veyra.game.endpoints import (
    DUNGEON_ARMY_TARGET_KEYS,
    THE_POLYHEDRAL_CRUCIBLE_NAME,
)

logger = logging.getLogger("veyra.dungeon_army")

DEFAULT_TICK_S = 30 * 60       # 30 minutes
SLEEP_TICK_S = 5               # responsiveness for stop checks


@dataclass
class DungeonArmyStatus:
    running: bool = False
    last_action_at: str | None = None
    last_action: str | None = None
    next_wake_at: str | None = None
    last_error: str | None = None
    progress: dict[str, str] = field(default_factory=dict)
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


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _pick_lowest_open_match(cards: list[dict]) -> dict | None:
    """Lowest match_no whose status != 'cleared'."""
    candidates = [c for c in cards if (c.get("status") or "").lower() != "cleared"]
    if not candidates:
        return None
    candidates.sort(key=lambda c: int(c.get("match_no", 0)))
    return candidates[0]


def _retreatable_mine(state: dict) -> list[dict]:
    captains = state.get("captains") or []
    return [
        c for c in captains
        if c.get("is_mine")
        and not c.get("is_dead")
        and not c.get("retreat_requested")
    ]


def _alive_enemy(state: dict) -> dict | None:
    for c in state.get("captains") or []:
        if (c.get("side") or "").upper() == "ENEMY" and not c.get("is_dead"):
            return c
    return None


async def _retreat_all(
    game: GameClient, status: DungeonArmyStatus, battle_id: int, key: str
) -> int:
    """Retreat every still-active captain we own. Returns count retreated."""
    state = await game.shadow_battle_state(battle_id)
    mine = _retreatable_mine(state)
    count = 0
    for cap in mine:
        cap_id = cap.get("captain_unit_id") or cap.get("id")
        if cap_id is None:
            continue
        try:
            r = await game.shadow_battle_retreat(battle_id, int(cap_id))
            if r.get("ok"):
                count += 1
            else:
                status.log(
                    f"[dungeon-army] {key}: retreat captain {cap_id} failed: "
                    f"{r.get('message') or r}"
                )
        except Exception as e:
            status.log(
                f"[dungeon-army] {key}: retreat captain {cap_id} error: {e}"
            )
    return count


async def _process_room(
    game: GameClient,
    status: DungeonArmyStatus,
    instance_id: str,
    node: dict,
) -> tuple[bool, bool]:
    """Run one army room. Returns (joined_a_fight, halt_cycle).

    halt_cycle=True means we used our one allowed join (or hit a hard server lock)
    and the cycle should stop trying further rooms.
    """
    key = node.get("key", "?")
    name = node.get("name", key)

    if node.get("status") == "hidden":
        status.log(f"[dungeon-army] {key}: status=hidden, skipping")
        status.progress[key] = "hidden"
        return False, False
    if node.get("is_cleared"):
        status.log(f"[dungeon-army] {key}: is_cleared, skipping")
        status.progress[key] = "cleared"
        return False, False

    node_id = node.get("id")
    face_key = node.get("face_key")
    if node_id is None or face_key is None:
        status.log(f"[dungeon-army] {key}: missing id/face_key in cube STATE")
        return False, False

    enter = await game.enter_cube_node(instance_id, node_id, face_key)
    if not enter.get("ok"):
        status.log(f"[dungeon-army] {key}: enter_node failed: {enter}")
        return False, False

    army = await game.fetch_army_node_state(instance_id, node_id)
    cards = army.get("cards") or []
    if not cards:
        status.log(f"[dungeon-army] {key}: no cards in army state, skipping")
        status.progress[key] = "no cards"
        return False, False

    chosen = _pick_lowest_open_match(cards)
    if not chosen:
        status.log(f"[dungeon-army] {key}: no open fight, skipping")
        status.progress[key] = "all cleared"
        return False, False

    match_no = int(chosen.get("match_no", 0))
    status.log(
        f"[dungeon-army] {key}: picked match #{match_no} "
        f"(status={chosen.get('status')})"
    )

    ef = await game.army_enter_fight(instance_id, node_id, match_no)
    if not ef.get("ok"):
        status.log(f"[dungeon-army] {key}: enter_fight failed: {ef}")
        status.progress[key] = f"enter_fight failed @ #{match_no}"
        return False, False

    battle_id = ef.get("battle_id")
    if not battle_id:
        status.log(f"[dungeon-army] {key}: enter_fight missing battle_id: {ef}")
        return False, False
    battle_id = int(battle_id)

    bs = await game.shadow_battle_state(battle_id)
    viewer = bs.get("viewer") or {}
    other = int(viewer.get("other_active_match_no") or 0)
    if other != 0:
        status.log(
            f"[dungeon-army] {key}: already engaged elsewhere (match #{other}), "
            f"halting cycle"
        )
        status.progress[key] = f"locked elsewhere (#{other})"
        return False, True

    captains = bs.get("captains") or []
    if any(c.get("is_mine") for c in captains):
        status.log(
            f"[dungeon-army] {key}: already participating in match #{match_no}, waiting"
        )
        status.progress[key] = f"already in #{match_no}"
        return False, True

    join = await game.shadow_battle_join(battle_id)
    if not join.get("ok"):
        msg = join.get("message") or join.get("error") or str(join)
        status.log(f"[dungeon-army] {key}: join_battle failed: {msg}")
        status.progress[key] = f"join failed @ #{match_no}"
        return False, True

    post_join_state = join.get("state") or await game.shadow_battle_state(battle_id)
    mine = _retreatable_mine(post_join_state)
    enemy = _alive_enemy(post_join_state)

    if not mine or not enemy:
        status.log(
            f"[dungeon-army] {key}: missing captain "
            f"(mine={len(mine)}, enemy={'yes' if enemy else 'no'}) "
            f"after join — retreating any joined captains"
        )
        retreated = await _retreat_all(game, status, battle_id, key)
        status.progress[key] = (
            f"joined #{match_no} but no target; retreated {retreated}"
        )
        return False, True

    attacker = mine[0]
    attacker_id = int(attacker.get("captain_unit_id") or attacker.get("id"))
    defender_id = int(enemy.get("captain_unit_id") or enemy.get("id"))

    assigned_ok = False
    try:
        at = await game.shadow_battle_assign_target(
            battle_id, attacker_id, defender_id
        )
        assigned_ok = bool(at.get("ok"))
        if not assigned_ok:
            status.log(
                f"[dungeon-army] {key}: assign_target failed: "
                f"{at.get('message') or at}"
            )
    except Exception as e:
        status.log(f"[dungeon-army] {key}: assign_target error: {e}")

    # Retreat all our captains immediately — no sleep. Even on assign failure
    # we must not leave captains stuck in the fight without retreat queued.
    retreated = await _retreat_all(game, status, battle_id, key)

    if assigned_ok:
        action_text = (
            f"joined {name} match #{match_no} (battle {battle_id}), "
            f"retreated {retreated} after 1 swing"
        )
    else:
        action_text = (
            f"joined {name} match #{match_no} (battle {battle_id}), "
            f"assign failed — retreated {retreated}"
        )
    status.log(f"[dungeon-army] {key}: {action_text}")
    status.last_action = action_text
    status.last_action_at = _now_iso()
    status.progress[key] = f"joined #{match_no}, retreated {retreated}"
    return True, True


async def _sleep_with_stop(status: DungeonArmyStatus, seconds: int) -> None:
    remaining = seconds
    while remaining > 0 and status.running:
        chunk = min(SLEEP_TICK_S, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def dungeon_army_worker(game: GameClient, status: DungeonArmyStatus) -> None:
    """Periodic loop: pick instance → for each army room, join one fight & retreat."""
    status.log("=== Guild Dungeon Army started ===")

    try:
        while status.running:
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    status.log, lambda: not status.running
                )
                if not recovered:
                    break

            status.log("[dungeon-army] cycle start")

            try:
                dungeons = await game.fetch_open_dungeons()
                game.record_net_success()
            except Exception as e:
                game.record_net_failure()
                status.last_error = f"fetch_open_dungeons: {e}"
                status.log(f"[dungeon-army] failed to fetch open dungeons: {e}")
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            crucible = next(
                (d for d in dungeons if d.get("name") == THE_POLYHEDRAL_CRUCIBLE_NAME),
                None,
            )
            if not crucible:
                status.log(
                    f"[dungeon-army] {THE_POLYHEDRAL_CRUCIBLE_NAME} not in Open Dungeons"
                )
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            instance_id = crucible["instance_id"]
            status.log(f"[dungeon-army] resolved instance_id={instance_id}")

            try:
                cube_state = await game.fetch_cube_state(instance_id)
                game.record_net_success()
            except Exception as e:
                game.record_net_failure()
                status.last_error = f"fetch_cube_state: {e}"
                status.log(f"[dungeon-army] failed to fetch cube state: {e}")
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            nodes_by_key = {n.get("key"): n for n in cube_state.get("nodes", [])}

            for key in DUNGEON_ARMY_TARGET_KEYS:
                if not status.running:
                    break
                node = nodes_by_key.get(key)
                if not node:
                    status.log(f"[dungeon-army] {key}: not in cube STATE, skipping")
                    status.progress[key] = "missing"
                    continue
                try:
                    _joined, halt = await _process_room(
                        game, status, instance_id, node
                    )
                    game.record_net_success()
                except Exception as e:
                    game.record_net_failure()
                    status.last_error = f"{key}: {e}"
                    status.log(
                        f"[dungeon-army] {key} ERROR: {type(e).__name__}: {e}"
                    )
                    halt = False
                if halt:
                    break

            sleep_s = DEFAULT_TICK_S
            wake_dt = datetime.now().timestamp() + sleep_s
            wake_iso = (
                datetime.fromtimestamp(wake_dt)
                .replace(microsecond=0).isoformat(sep=" ")
            )
            status.next_wake_at = wake_iso
            status.log(f"[dungeon-army] sleeping {sleep_s}s (until {wake_iso})")
            await _sleep_with_stop(status, sleep_s)

    except Exception as e:
        logger.error("dungeon-army fatal: %s", e, exc_info=True)
        status.last_error = str(e)
        status.log(f"[dungeon-army] fatal error: {e}")
    finally:
        status.log("=== Guild Dungeon Army stopped ===")
        status.stop()
