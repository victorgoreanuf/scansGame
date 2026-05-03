"""Guild Dungeon Warrens worker — Gribble Junk-Magus leech for Full Stamina Potions."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from veyra.game.client import GameClient
from veyra.game.endpoints import (
    SHADOWBRIDGE_WARRENS_NAME,
    WARRENS_DAMAGE_THRESHOLD,
    WARRENS_GRIBBLE_LOCATIONS,
    WARRENS_GRIBBLE_NAME,
    WARRENS_STAMINA_PER_HIT,
    WARRENS_STAMINA_SKILL_ID,
)
from veyra.game.parser import (
    parse_dungeon_battle_status,
    parse_my_dungeon_damage,
)
from veyra.game.types import WarrensMonsterCard

logger = logging.getLogger("veyra.dungeon_warrens")

DEFAULT_TICK_S = 3 * 60 * 60
SLEEP_TICK_S = 5
HIT_CAP_PER_GRIBBLE = 10


@dataclass
class DungeonWarrensStatus:
    running: bool = False
    last_action_at: str | None = None
    last_action: str | None = None
    next_wake_at: str | None = None
    last_error: str | None = None
    progress: dict[str, str] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
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


def _fmt(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n:,}"


async def _farm_one_gribble(
    game: GameClient,
    status: DungeonWarrensStatus,
    instance_id: str,
    gribble: WarrensMonsterCard,
) -> tuple[str, int]:
    """Run the per-Gribble picker. Returns (outcome, hits_fired).

    outcome ∈ {"already", "dead", "stamina", "joined", "farmed", "error"}.
    """
    dgmid = gribble.dgmid
    try:
        page = await game.fetch_dungeon_battle_page(instance_id, dgmid)
    except Exception as e:
        status.log(f"[dungeon-warrens] dgmid={dgmid}: fetch battle page error: {e}")
        return "error", 0

    my_damage = parse_my_dungeon_damage(page, game.user_id)
    bs = parse_dungeon_battle_status(page)
    stamina = bs.get("stamina")
    joined = bool(bs.get("joined"))
    monster_alive = bool(bs.get("monster_alive"))

    status.log(
        f"[dungeon-warrens] dgmid={dgmid}: pre my_damage={_fmt(my_damage)}, "
        f"stamina={_fmt(stamina)}, joined={'yes' if joined else 'no'}, "
        f"alive={'yes' if monster_alive else 'no'}"
    )

    if my_damage >= WARRENS_DAMAGE_THRESHOLD:
        status.log(
            f"[dungeon-warrens] dgmid={dgmid}: skip — my damage "
            f"{_fmt(my_damage)} ≥ {_fmt(WARRENS_DAMAGE_THRESHOLD)}"
        )
        return "already", 0
    if not monster_alive:
        status.log(f"[dungeon-warrens] dgmid={dgmid}: skip — monster dead")
        return "dead", 0
    if stamina is None or stamina < WARRENS_STAMINA_PER_HIT:
        status.log(
            f"[dungeon-warrens] dgmid={dgmid}: skip — stamina "
            f"{_fmt(stamina)} < {WARRENS_STAMINA_PER_HIT}, leaving for next cycle"
        )
        return "stamina", 0

    if not joined:
        ok, msg = await game.join_dungeon_battle(instance_id, dgmid)
        if not ok:
            status.log(
                f"[dungeon-warrens] dgmid={dgmid}: join failed: {msg[:120]!r}"
            )
            return "error", 0
        status.log(f"[dungeon-warrens] dgmid={dgmid}: joined ({msg[:80]!r})")

    cumulative = my_damage
    hits = 0
    for _ in range(HIT_CAP_PER_GRIBBLE):
        if cumulative >= WARRENS_DAMAGE_THRESHOLD:
            break
        if stamina is None or stamina < WARRENS_STAMINA_PER_HIT:
            status.log(
                f"[dungeon-warrens] dgmid={dgmid}: stop — stamina "
                f"{_fmt(stamina)} < {WARRENS_STAMINA_PER_HIT}"
            )
            return "stamina", hits

        try:
            resp = await game.attack_dungeon_monster(
                instance_id, dgmid,
                stamina_cost=WARRENS_STAMINA_PER_HIT,
                skill_id=WARRENS_STAMINA_SKILL_ID,
            )
        except Exception as e:
            status.log(f"[dungeon-warrens] dgmid={dgmid}: attack error: {e}")
            return "error", hits

        if not isinstance(resp, dict) or "totaldmgdealt" not in resp:
            status.log(
                f"[dungeon-warrens] dgmid={dgmid}: unexpected attack response: "
                f"{str(resp)[:160]}"
            )
            return "error", hits

        try:
            cumulative = int(resp.get("totaldmgdealt") or cumulative)
        except (TypeError, ValueError):
            pass

        if "stamina_left" in resp:
            try:
                stamina = int(resp["stamina_left"])
            except (TypeError, ValueError):
                pass
        elif "stamina" in resp:
            try:
                stamina = int(resp["stamina"])
            except (TypeError, ValueError):
                pass
        else:
            stamina = (stamina or 0) - WARRENS_STAMINA_PER_HIT

        monster_dead = bool(resp.get("monsterdead") or resp.get("monster_dead"))
        hits += 1
        done = cumulative >= WARRENS_DAMAGE_THRESHOLD
        tail = " — DONE" if done else ""
        if monster_dead and not done:
            tail = " — monster died"
        status.log(
            f"[dungeon-warrens] dgmid={dgmid}: hit #{hits} "
            f"totaldmgdealt={_fmt(cumulative)} stamina_left={_fmt(stamina)}{tail}"
        )
        if done or monster_dead:
            break
    else:
        status.log(
            f"[dungeon-warrens] dgmid={dgmid}: hit cap {HIT_CAP_PER_GRIBBLE} "
            f"reached (totaldmgdealt={_fmt(cumulative)})"
        )

    if cumulative >= WARRENS_DAMAGE_THRESHOLD:
        status.last_action = (
            f"Gribble dgmid={dgmid}: my_damage={_fmt(cumulative)} after {hits} hits"
        )
        status.last_action_at = _now_iso()
        return "farmed", hits
    return "joined", hits


async def _process_location(
    game: GameClient,
    status: DungeonWarrensStatus,
    instance_id: str,
    location_id: int,
) -> None:
    key = f"loc {location_id}"
    try:
        _html, cards = await game.fetch_warrens_room(instance_id, location_id)
    except Exception as e:
        status.log(f"[dungeon-warrens] {key}: fetch room error: {e}")
        status.progress[key] = "fetch error"
        return

    gribbles = [
        c for c in cards
        if c.name == WARRENS_GRIBBLE_NAME and not c.is_dead
    ]
    status.log(
        f"[dungeon-warrens] {key}: {len(gribbles)} alive Gribbles"
    )
    if not gribbles:
        status.progress[key] = "0 Gribbles"
        return

    farmed = 0
    already = 0
    skipped_stamina = 0
    skipped_dead = 0
    errors = 0

    for gribble in gribbles:
        if not status.running:
            break
        outcome, _hits = await _farm_one_gribble(
            game, status, instance_id, gribble
        )
        if outcome == "farmed":
            farmed += 1
        elif outcome == "already":
            already += 1
        elif outcome == "stamina":
            skipped_stamina += 1
            status.progress[key] = (
                f"{farmed}/{len(gribbles)} farmed (stamina out)"
            )
            status.log(
                f"[dungeon-warrens] {key}: stamina exhausted, halting room"
            )
            break
        elif outcome == "dead":
            skipped_dead += 1
        elif outcome == "error":
            errors += 1
        elif outcome == "joined":
            farmed += 0  # joined but didn't reach threshold (rare; bound exhausted)

    status.progress[key] = (
        f"{farmed}/{len(gribbles)} farmed "
        f"({already} ≥1M, {skipped_stamina} stamina, "
        f"{skipped_dead} dead, {errors} err)"
    )
    status.totals["farmed"] = status.totals.get("farmed", 0) + farmed
    status.totals["already"] = status.totals.get("already", 0) + already
    status.totals["stamina_skipped"] = (
        status.totals.get("stamina_skipped", 0) + skipped_stamina
    )
    status.log(
        f"[dungeon-warrens] {key}: {farmed}/{len(gribbles)} farmed this cycle "
        f"({already} already ≥1M, {skipped_stamina} skipped: stamina, "
        f"{skipped_dead} dead, {errors} errors)"
    )


async def _sleep_with_stop(status: DungeonWarrensStatus, seconds: int) -> None:
    remaining = seconds
    while remaining > 0 and status.running:
        chunk = min(SLEEP_TICK_S, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def dungeon_warrens_worker(
    game: GameClient, status: DungeonWarrensStatus
) -> None:
    """3h cycle: find Shadowbridge Warrens → loc 2 + 4 → leech each Gribble to ≥1M."""
    status.log("=== Guild Dungeon Warrens started ===")

    try:
        while status.running:
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    status.log, lambda: not status.running
                )
                if not recovered:
                    break

            status.log("[dungeon-warrens] cycle start")

            try:
                dungeons = await game.fetch_open_dungeons()
                game.record_net_success()
            except Exception as e:
                game.record_net_failure()
                status.last_error = f"fetch_open_dungeons: {e}"
                status.log(
                    f"[dungeon-warrens] failed to fetch open dungeons: {e}"
                )
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            warrens = next(
                (d for d in dungeons if d.get("name") == SHADOWBRIDGE_WARRENS_NAME),
                None,
            )
            if not warrens:
                status.log(
                    f"[dungeon-warrens] {SHADOWBRIDGE_WARRENS_NAME} not in Open Dungeons"
                )
                await _sleep_with_stop(status, DEFAULT_TICK_S)
                continue

            instance_id = warrens["instance_id"]
            status.log(
                f"[dungeon-warrens] resolved {SHADOWBRIDGE_WARRENS_NAME} "
                f"instance_id={instance_id}"
            )

            for location_id in WARRENS_GRIBBLE_LOCATIONS:
                if not status.running:
                    break
                try:
                    await _process_location(game, status, instance_id, location_id)
                    game.record_net_success()
                except Exception as e:
                    game.record_net_failure()
                    status.last_error = f"loc {location_id}: {e}"
                    status.log(
                        f"[dungeon-warrens] loc {location_id} ERROR: "
                        f"{type(e).__name__}: {e}"
                    )

            sleep_s = DEFAULT_TICK_S
            wake_dt = datetime.now().timestamp() + sleep_s
            wake_iso = (
                datetime.fromtimestamp(wake_dt)
                .replace(microsecond=0).isoformat(sep=" ")
            )
            status.next_wake_at = wake_iso
            status.log(f"[dungeon-warrens] sleeping {sleep_s}s (until {wake_iso})")
            await _sleep_with_stop(status, sleep_s)

    except Exception as e:
        logger.error("dungeon-warrens fatal: %s", e, exc_info=True)
        status.last_error = str(e)
        status.log(f"[dungeon-warrens] fatal error: {e}")
    finally:
        status.log("=== Guild Dungeon Warrens stopped ===")
        status.stop()
