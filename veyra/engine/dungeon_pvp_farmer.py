"""Guild Dungeon PvP worker — joins PvP-style matches in The Polyhedral Crucible."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from veyra.game.client import GameClient
from veyra.game.endpoints import (
    DUNGEON_PVP_TARGET_KEYS,
    THE_POLYHEDRAL_CRUCIBLE_NAME,
)
from veyra.game.types import PvpNodeMatchCard

logger = logging.getLogger("veyra.dungeon_pvp")

DEFAULT_TICK_S = 30 * 60       # 30 minutes
COOLDOWN_PAD_S = 60            # wake `pad` seconds after cooldown expiry
SLEEP_TICK_S = 5               # responsiveness for stop checks


@dataclass
class DungeonPvpStatus:
    running: bool = False
    last_action_at: str | None = None
    last_action: str | None = None
    next_wake_at: str | None = None
    cooldowns: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None
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


def _pick_match(cards: list[PvpNodeMatchCard]) -> PvpNodeMatchCard | None:
    """Highest slots in [1..4] (ties → lowest match_no), else lowest match_no with 0 slots."""
    partial = [c for c in cards if c.status == "OPEN" and 1 <= c.slots <= 4]
    if partial:
        partial.sort(key=lambda c: (-c.slots, c.match_no))
        return partial[0]
    empty = [c for c in cards if c.status == "OPEN" and c.slots == 0]
    if empty:
        empty.sort(key=lambda c: c.match_no)
        return empty[0]
    return None


def _empty_ally_slots(state: dict) -> list[int]:
    """Return ascending list of empty slot indices (1..5) in teams.ally.players_by_num.

    Handles dual encoding: missing key OR `null` value both mean empty. As a defensive
    fallback, also treat present-but-zeroed entries (user_id == 0 AND npc_id == 0) as empty.
    """
    ally = ((state.get("teams") or {}).get("ally") or {}).get("players_by_num") or {}
    empties: list[int] = []
    for slot in range(1, 6):
        v = ally.get(str(slot))
        if v is None:
            empties.append(slot)
            continue
        if isinstance(v, dict) and v.get("user_id") == 0 and v.get("npc_id") == 0:
            empties.append(slot)
    return empties


async def _process_room(
    game: GameClient,
    status: DungeonPvpStatus,
    instance_id: str,
    node: dict,
) -> int | None:
    """Run the picker for one room. Returns cooldown seconds remaining (or None)."""
    key = node.get("key", "?")
    name = node.get("name", key)

    if node.get("status") == "hidden":
        status.log(f"[dungeon-pvp] {key}: status=hidden, skipping")
        return None
    if node.get("is_cleared"):
        status.log(f"[dungeon-pvp] {key}: is_cleared, skipping")
        return None

    node_id = node.get("id")
    face_key = node.get("face_key")
    if node_id is None or face_key is None:
        status.log(f"[dungeon-pvp] {key}: missing id/face_key in cube STATE")
        return None

    enter = await game.enter_cube_node(instance_id, node_id, face_key)
    if not enter.get("ok"):
        status.log(f"[dungeon-pvp] {key}: enter_node failed: {enter}")
        return None

    cards, cooldown = await game.fetch_pvp_node_matches(instance_id, node_id)
    if cooldown is not None:
        h, rem = divmod(cooldown, 3600)
        m, s = divmod(rem, 60)
        status.log(
            f"[dungeon-pvp] {key}: cooldown {h:02d}:{m:02d}:{s:02d} remaining"
        )
        return cooldown

    open_cnt = sum(1 for c in cards if c.status == "OPEN")
    live_cnt = sum(1 for c in cards if c.status == "LIVE")
    cleared_cnt = sum(1 for c in cards if c.status == "CLEARED")
    status.log(
        f"[dungeon-pvp] {key}: status={node.get('status')}, "
        f"matches: {open_cnt} OPEN / {live_cnt} LIVE / {cleared_cnt} CLEARED"
    )

    chosen = _pick_match(cards)
    if not chosen:
        status.log(f"[dungeon-pvp] {key}: no joinable candidate, skipping")
        return None

    status.log(
        f"[dungeon-pvp] {key}: picker chose match #{chosen.match_no} "
        f"({chosen.slots}/{chosen.cap})"
    )

    pre_state = await game.fetch_pvp_match_state(instance_id, node_id, chosen.match_no)
    if pre_state.get("room_joined"):
        status.log(
            f"[dungeon-pvp] {key}: already joined match #{chosen.match_no}, skipping room"
        )
        return None

    empties = _empty_ally_slots(pre_state)
    if not empties:
        status.log(
            f"[dungeon-pvp] {key}: match #{chosen.match_no} has no empty ally slot"
        )
        return None

    slot_index = empties[0]
    join_resp = await game.pvp_pick_slot(
        instance_id, node_id, chosen.match_no, slot_index
    )
    if not join_resp.get("ok"):
        msg = join_resp.get("message") or join_resp.get("error") or str(join_resp)
        status.log(f"[dungeon-pvp] {key}: pick_slot failed: {msg}")
        return None

    msg = join_resp.get("message", "joined")
    action_text = (
        f"joined {name} match #{chosen.match_no} slot {slot_index} — {msg}"
    )
    status.log(f"[dungeon-pvp] {key}: {action_text}")
    status.last_action = action_text
    status.last_action_at = _now_iso()

    # Re-read the node page to capture the cooldown that the join just produced.
    _, cooldown_after = await game.fetch_pvp_node_matches(instance_id, node_id)
    if cooldown_after is not None:
        h, rem = divmod(cooldown_after, 3600)
        m, s = divmod(rem, 60)
        status.log(
            f"[dungeon-pvp] {key}: post-join cooldown {h:02d}:{m:02d}:{s:02d}"
        )
    return cooldown_after


def _compute_next_wakeup_seconds(cooldowns: dict[str, int]) -> int:
    """If any cooldown is < 30 min, wake just after its expiry; else default tick."""
    if not cooldowns:
        return DEFAULT_TICK_S
    soonest = min(cooldowns.values())
    if soonest < DEFAULT_TICK_S:
        return soonest + COOLDOWN_PAD_S
    return DEFAULT_TICK_S


async def _sleep_with_stop(status: DungeonPvpStatus, seconds: int) -> None:
    remaining = seconds
    while remaining > 0 and status.running:
        chunk = min(SLEEP_TICK_S, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def dungeon_pvp_worker(game: GameClient, status: DungeonPvpStatus) -> None:
    """Periodic loop: pick instance → for each target room, enter + try to join → sleep."""
    status.log("=== Guild Dungeon PvP started ===")

    try:
        while status.running:
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    status.log, lambda: not status.running
                )
                if not recovered:
                    break

            cycle_cooldowns: dict[str, int] = {}
            status.log("[dungeon-pvp] cycle start")

            try:
                dungeons = await game.fetch_open_dungeons()
                game.record_net_success()
            except Exception as e:
                game.record_net_failure()
                status.last_error = f"fetch_open_dungeons: {e}"
                status.log(f"[dungeon-pvp] failed to fetch open dungeons: {e}")
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            crucible = next(
                (d for d in dungeons if d.get("name") == THE_POLYHEDRAL_CRUCIBLE_NAME),
                None,
            )
            if not crucible:
                status.log(
                    f"[dungeon-pvp] {THE_POLYHEDRAL_CRUCIBLE_NAME} not in Open Dungeons"
                )
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            instance_id = crucible["instance_id"]
            status.log(f"[dungeon-pvp] resolved instance_id={instance_id}")

            try:
                cube_state = await game.fetch_cube_state(instance_id)
                game.record_net_success()
            except Exception as e:
                game.record_net_failure()
                status.last_error = f"fetch_cube_state: {e}"
                status.log(f"[dungeon-pvp] failed to fetch cube state: {e}")
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            nodes_by_key = {n.get("key"): n for n in cube_state.get("nodes", [])}

            for key in DUNGEON_PVP_TARGET_KEYS:
                if not status.running:
                    break
                node = nodes_by_key.get(key)
                if not node:
                    status.log(f"[dungeon-pvp] {key}: not in cube STATE, skipping")
                    continue
                try:
                    cd = await _process_room(game, status, instance_id, node)
                    game.record_net_success()
                except Exception as e:
                    game.record_net_failure()
                    status.last_error = f"{key}: {e}"
                    status.log(f"[dungeon-pvp] {key} ERROR: {type(e).__name__}: {e}")
                    cd = None
                if cd is not None:
                    cycle_cooldowns[key] = cd

            status.cooldowns = dict(cycle_cooldowns)
            sleep_s = _compute_next_wakeup_seconds(cycle_cooldowns)
            wake_dt = datetime.now().timestamp() + sleep_s
            wake_iso = datetime.fromtimestamp(wake_dt).replace(microsecond=0).isoformat(sep=" ")
            status.next_wake_at = wake_iso
            status.log(f"[dungeon-pvp] sleeping {sleep_s}s (until {wake_iso})")
            await _sleep_with_stop(status, sleep_s)

    except Exception as e:
        logger.error("dungeon-pvp fatal: %s", e, exc_info=True)
        status.last_error = str(e)
        status.log(f"[dungeon-pvp] fatal error: {e}")
    finally:
        status.log("=== Guild Dungeon PvP stopped ===")
        status.stop()
