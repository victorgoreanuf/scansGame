"""Account worker lifecycle and orchestration."""

import asyncio
import time
from dataclasses import dataclass, field

from veyra.game.client import GameClient
from veyra.game.types import MonsterGroup, TargetConfig
from veyra.engine.rate_limiter import RateLimiter
from veyra.engine.wave_farmer import FarmerState, worker
from veyra.engine.pvp_fighter import PvPState, pvp_worker


@dataclass
class AccountWorker:
    """Represents one running account with its own game client and state."""
    game: GameClient
    state: FarmerState = field(default_factory=FarmerState)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    task: asyncio.Task | None = None
    connected: bool = False
    user_id: str = ""
    waves: dict[int, list[MonsterGroup]] = field(default_factory=dict)
    # PvP
    pvp_state: PvPState = field(default_factory=PvPState)
    pvp_task: asyncio.Task | None = None


class AccountManager:
    """Manages the single-account worker (multi-account in Phase 4)."""

    def __init__(self):
        self._worker: AccountWorker | None = None

    @property
    def worker(self) -> AccountWorker | None:
        return self._worker

    @property
    def is_running(self) -> bool:
        if self._worker is None:
            return False
        # Check both the state flag AND whether the asyncio task is still alive
        if self._worker.task and self._worker.task.done():
            self._worker.state.running = False
            return False
        return self._worker.state.running

    @property
    def is_connected(self) -> bool:
        return self._worker is not None and self._worker.connected

    async def connect(self, email: str, password: str) -> tuple[bool, dict[int, list[MonsterGroup]]]:
        """Login and fetch wave data. Returns (success, waves_grouped)."""
        if self._worker and self._worker.state.running:
            self._worker.state.stop()
            if self._worker.task:
                self._worker.task.cancel()

        game = GameClient()
        w = AccountWorker(game=game)
        self._worker = w

        w.state.log("Logging in...")
        try:
            ok = await game.login(email, password)
            w.state.log("Login successful!" if ok else "Login may have failed, continuing...")
        except Exception as e:
            w.state.log(f"Login error: {e}")
            return False, {}

        waves: dict[int, list[MonsterGroup]] = {}
        for wn in [1, 2]:
            w.state.log(f"Fetching Wave {wn}...")
            try:
                grouped = await game.fetch_wave_grouped(wn)
                waves[wn] = grouped
                total = sum(g.count for g in grouped)
                w.state.log(f"  {len(grouped)} types, {total} monsters alive")
            except Exception as e:
                w.state.log(f"  Failed: {e}")
                waves[wn] = []

        w.waves = waves
        w.user_id = game.user_id
        w.connected = True
        w.state.log("Ready!")
        return True, waves

    async def start(self, targets: list[TargetConfig]) -> bool:
        """Start the farming worker with given targets."""
        if not self._worker or not self._worker.connected:
            return False
        if self._worker.task and not self._worker.task.done():
            return False

        w = self._worker
        w.state = FarmerState()
        w.state.running = True
        w.state.stats.started_at = time.time()
        w.limiter.reset()

        w.task = asyncio.create_task(
            worker(w.game, targets, w.state, w.limiter)
        )
        return True

    def stop(self) -> None:
        """Signal the worker to stop."""
        if self._worker and self._worker.state.running:
            self._worker.state.stop()
            self._worker.state.log("Stopping...")

    # ── PvP ────────────────────────────────────────────────────────────────

    @property
    def is_pvp_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.pvp_task and self._worker.pvp_task.done():
            self._worker.pvp_state.running = False
            return False
        return self._worker.pvp_state.running

    async def start_pvp(self) -> bool:
        """Start the PvP auto-fight worker."""
        if not self._worker or not self._worker.connected:
            return False
        if self._worker.pvp_task and not self._worker.pvp_task.done():
            return False

        w = self._worker
        w.pvp_state = PvPState()
        w.pvp_state.running = True

        w.pvp_task = asyncio.create_task(pvp_worker(w.game, w.pvp_state))
        return True

    def stop_pvp(self) -> None:
        """Signal the PvP worker to stop."""
        if self._worker and self._worker.pvp_state.running:
            self._worker.pvp_state.stop()
            self._worker.pvp_state.log("Stopping PvP...")

    def get_pvp_state(self) -> PvPState | None:
        if self._worker:
            return self._worker.pvp_state
        return None

    def get_pvp_stats(self) -> dict:
        if not self._worker:
            return {"matches": 0, "wins": 0, "losses": 0, "tokens": 0}
        s = self._worker.pvp_state
        return {
            "matches": s.matches_played,
            "wins": s.wins,
            "losses": s.losses,
            "tokens": s.tokens_remaining,
        }

    def get_state(self) -> FarmerState | None:
        if self._worker:
            return self._worker.state
        return None

    def get_stats(self) -> dict:
        if not self._worker:
            return {"killed": 0, "damage": 0, "stamina": 0, "start_time": 0, "looted": 0}
        s = self._worker.state.stats
        return {
            "killed": s.killed,
            "damage": s.damage,
            "stamina": s.stamina_spent,
            "start_time": getattr(s, "started_at", 0),
            "looted": s.looted,
        }

    async def cleanup(self) -> None:
        """Shutdown: stop worker and close client."""
        if self._worker:
            self._worker.state.stop()
            self._worker.pvp_state.stop()
            if self._worker.task:
                self._worker.task.cancel()
                try:
                    await self._worker.task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._worker.pvp_task:
                self._worker.pvp_task.cancel()
                try:
                    await self._worker.pvp_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._worker.game.close()
            self._worker = None


# Singleton for the app
manager = AccountManager()
