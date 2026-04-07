"""PvP auto-fight engine — finds solo matches and auto-plays them."""

import asyncio
import logging
from dataclasses import dataclass, field

from veyra.game.client import GameClient

logger = logging.getLogger("veyra.pvp")

POLL_INTERVAL = 3  # seconds between state polls
MATCH_COOLDOWN = 5  # seconds between matches


@dataclass
class PvPState:
    running: bool = False
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    tokens_remaining: int = 0
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


async def pvp_worker(game: GameClient, state: PvPState) -> None:
    """Main PvP loop: find match -> auto-play -> poll until done -> repeat."""
    state.log("=== PvP Auto-Fight Started ===")

    try:
        # Check initial token count
        try:
            tokens = await game.fetch_pvp_tokens()
            state.tokens_remaining = tokens
            state.log(f"Solo tokens available: {tokens}")
        except Exception as e:
            state.log(f"Failed to check PvP tokens: {e}")
            state.log("Continuing anyway...")
            tokens = -1  # unknown

        if tokens == 0:
            state.log("No solo tokens available. Stopping.")
            state.stop()
            return

        while state.running:
            # Wait for site recovery if it's down
            if game.is_site_down:
                recovered = await game.wait_for_site_up(
                    state.log, lambda: not state.running
                )
                if not recovered:
                    break

            if tokens == 0:
                state.log("Out of solo tokens. Stopping PvP.")
                break

            state.log("")
            state.log(f"--- Finding solo match (tokens: {tokens if tokens >= 0 else '?'}) ---")

            # 1. Queue for matchmaking
            try:
                mm_result = await game.pvp_find_match("solo")
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
                if "token" in err_lower or "no tokens" in err_lower:
                    state.log("No tokens remaining. Stopping PvP.")
                    break

                await _sleep(state, 10)
                continue

            match_id = str(mm_result.get("match_id", ""))
            if not match_id:
                state.log(f"No match_id in response: {mm_result}")
                await _sleep(state, 10)
                continue

            state.log(f"Match found! ID: {match_id}")

            # 2. Set auto-play mode
            await asyncio.sleep(2)
            try:
                await game.pvp_set_auto(match_id)
                state.log("Auto-play enabled")
            except Exception as e:
                state.log(f"Failed to set auto-play: {e}")

            # 3. Poll until battle ends
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

                # Update log cursor
                if "last_log_id" in data:
                    try:
                        since_log_id = int(data["last_log_id"])
                    except (ValueError, TypeError):
                        pass

                # Check match.ended flag
                match_obj = data.get("match", {})
                if match_obj.get("ended"):
                    battle_done = True
                    state.matches_played += 1

                    winner_side = match_obj.get("winner_side", "")
                    our_side = data.get("viewer", {}).get("side", "ally")

                    if winner_side == our_side:
                        state.wins += 1
                        state.log(f"Battle #{state.matches_played} WON!")
                    elif winner_side:
                        state.losses += 1
                        state.log(f"Battle #{state.matches_played} LOST")
                    else:
                        state.log(f"Battle #{state.matches_played} finished")

                    # Log reward if present
                    reward = data.get("reward_summary", {})
                    if reward.get("show") and reward.get("text"):
                        state.log(f"  Reward: {reward['text']}")

            if not state.running:
                break

            # 4. Re-check tokens
            try:
                tokens = await game.fetch_pvp_tokens()
                state.tokens_remaining = tokens
            except Exception:
                if tokens > 0:
                    tokens -= 1
                    state.tokens_remaining = tokens

            # Cooldown between matches
            state.log(f"Waiting {MATCH_COOLDOWN}s before next match...")
            await _sleep(state, MATCH_COOLDOWN)

    except Exception as e:
        logger.error("PvP worker fatal error: %s", e, exc_info=True)
        state.log(f"PvP fatal error: {e}")
    finally:
        state.log("")
        state.log(f"=== PvP Stopped ({state.matches_played} played, {state.wins}W/{state.losses}L) ===")
        state.stop()


async def _sleep(state: PvPState, seconds: int) -> None:
    """Sleep in 1s increments for responsive stopping."""
    for _ in range(seconds):
        if not state.running:
            break
        await asyncio.sleep(1)
