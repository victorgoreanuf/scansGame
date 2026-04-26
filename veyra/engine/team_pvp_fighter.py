"""Team (party) PvP auto-fight engine — only runnable by the party leader."""

import asyncio
import logging
from dataclasses import dataclass, field

from veyra.game.client import GameClient

logger = logging.getLogger("veyra.team_pvp")

POLL_INTERVAL = 3   # seconds between battle state polls
MATCH_COOLDOWN = 5  # seconds between matches


@dataclass
class TeamPvPState:
    running: bool = False
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    tokens_remaining: int = 0
    tokens_max: int = 0
    is_leader: bool = False
    in_party: bool = False
    party_name: str = ""
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


async def team_pvp_worker(game: GameClient, state: TeamPvPState) -> None:
    """Main team-PvP loop: verify leadership -> find party match -> auto-play -> repeat."""
    state.log("=== Team PvP Auto-Fight Started ===")

    try:
        try:
            status = await game.fetch_pvp_party_status()
            game.record_net_success()
        except Exception as e:
            state.log(f"Failed to read party status: {e}")
            game.record_net_failure()
            return

        state.in_party = bool(status.get("in_party"))
        state.is_leader = bool(status.get("is_leader"))
        state.tokens_remaining = int(status.get("tokens", 0))
        state.tokens_max = int(status.get("tokens_max", 0))
        state.party_name = str(status.get("party_name", "") or "")

        if not state.in_party:
            state.log("You're not in a party. Stopping.")
            return
        if not state.is_leader:
            state.log("Only the party leader can start team matches. Stopping.")
            return

        token_str = (
            f"{state.tokens_remaining}/{state.tokens_max}"
            if state.tokens_max
            else str(state.tokens_remaining)
        )
        state.log(f"Party tokens available: {token_str}")
        if state.tokens_remaining == 0:
            state.log("No party tokens available. Stopping.")
            return

        while state.running:
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    state.log, lambda: not state.running
                )
                if not recovered:
                    break

            if state.tokens_remaining == 0:
                state.log("Out of party tokens. Stopping team PvP.")
                break

            state.log("")
            state.log(
                f"--- Finding party match (tokens: {state.tokens_remaining}"
                + (f"/{state.tokens_max}" if state.tokens_max else "")
                + ") ---"
            )

            try:
                mm_result = await game.pvp_find_match("party")
                game.record_net_success()
            except Exception as e:
                state.log(f"Matchmaking error: {e}")
                game.record_net_failure()
                await _sleep(state, 10)
                continue

            if "error" in mm_result and not mm_result.get("match_id"):
                err = mm_result.get("error", mm_result.get("message", str(mm_result)))
                state.log(f"Matchmaking failed: {err}")

                err_lower = str(err).lower()
                if "token" in err_lower:
                    state.log("No party tokens remaining. Stopping team PvP.")
                    break
                if "leader" in err_lower or "permission" in err_lower:
                    state.log("Lost leader permission. Stopping team PvP.")
                    break

                await _sleep(state, 10)
                continue

            match_id = str(mm_result.get("match_id", ""))
            if not match_id:
                state.log(f"No match_id in response: {mm_result}")
                await _sleep(state, 10)
                continue

            state.log(f"Party match found! ID: {match_id}")

            await asyncio.sleep(2)
            try:
                await game.pvp_set_party_auto(match_id, enabled=True)
                state.log("Party auto-play enabled")
            except Exception as e:
                state.log(f"Failed to set party auto-play: {e}")

            since_log_id = 0
            battle_done = False
            poll_errors = 0

            while state.running and not battle_done:
                await asyncio.sleep(POLL_INTERVAL)

                try:
                    data = await game.pvp_poll_state(match_id, since_log_id)
                    poll_errors = 0
                    game.record_net_success()
                except Exception as e:
                    poll_errors += 1
                    game.record_net_failure()
                    state.log(f"Poll error: {e}")
                    if game.is_site_down or poll_errors >= 5:
                        state.log("Too many poll errors, assuming battle ended.")
                        battle_done = True
                    continue

                if "last_log_id" in data:
                    try:
                        since_log_id = int(data["last_log_id"])
                    except (ValueError, TypeError):
                        pass

                match_obj = data.get("match", {})
                if match_obj.get("ended"):
                    battle_done = True
                    state.matches_played += 1

                    winner_side = match_obj.get("winner_side", "")
                    our_side = data.get("viewer", {}).get("side", "ally")

                    if winner_side == our_side:
                        state.wins += 1
                        state.log(f"Party battle #{state.matches_played} WON!")
                    elif winner_side:
                        state.losses += 1
                        state.log(f"Party battle #{state.matches_played} LOST")
                    else:
                        state.log(f"Party battle #{state.matches_played} finished")

                    reward = data.get("reward_summary", {})
                    if reward.get("show") and reward.get("text"):
                        state.log(f"  Reward: {reward['text']}")

            if not state.running:
                break

            try:
                status = await game.fetch_pvp_party_status()
                state.tokens_remaining = int(status.get("tokens", 0))
                if status.get("tokens_max"):
                    state.tokens_max = int(status["tokens_max"])
            except Exception:
                if state.tokens_remaining > 0:
                    state.tokens_remaining -= 1

            state.log(f"Waiting {MATCH_COOLDOWN}s before next match...")
            await _sleep(state, MATCH_COOLDOWN)

    except Exception as e:
        logger.error("Team PvP worker fatal error: %s", e, exc_info=True)
        state.log(f"Team PvP fatal error: {e}")
    finally:
        state.log("")
        state.log(
            f"=== Team PvP Stopped ({state.matches_played} played, "
            f"{state.wins}W/{state.losses}L) ==="
        )
        state.stop()


async def _sleep(state: TeamPvPState, seconds: int) -> None:
    for _ in range(seconds):
        if not state.running:
            break
        await asyncio.sleep(1)
