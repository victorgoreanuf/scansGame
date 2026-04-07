"""Account worker lifecycle and orchestration."""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from veyra.game.client import GameClient
from veyra.game.types import MonsterGroup, TargetConfig
from veyra.engine.rate_limiter import RateLimiter
from veyra.engine.wave_farmer import FarmerState, worker
from veyra.engine.pvp_fighter import PvPState, pvp_worker
from veyra.engine.stat_allocator import StatAllocatorState, stat_allocator_worker
from veyra.engine.quest_runner import QuestState, quest_worker


logger = logging.getLogger("veyra.manager")

PVP_CHECK_INTERVAL = 3600  # 1 hour


@dataclass
class AccountWorker:
    """Represents one running account with its own game client and state."""
    game: GameClient
    email: str = ""
    state: FarmerState = field(default_factory=FarmerState)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    task: asyncio.Task | None = None
    connected: bool = False
    user_id: str = ""
    waves: dict[int, list[MonsterGroup]] = field(default_factory=dict)
    # PvP
    pvp_state: PvPState = field(default_factory=PvPState)
    pvp_task: asyncio.Task | None = None
    pvp_auto_task: asyncio.Task | None = None  # background hourly PvP check
    # Stat allocator
    stat_state: StatAllocatorState = field(default_factory=StatAllocatorState)
    stat_task: asyncio.Task | None = None
    # Quest runner
    quest_state: QuestState = field(default_factory=QuestState)
    quest_task: asyncio.Task | None = None


class AccountManager:
    """Manages the single-account worker (multi-account in Phase 4)."""

    def __init__(self):
        self._worker: AccountWorker | None = None
        self._session_token: str | None = None

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
    def game_client(self) -> GameClient | None:
        return self._worker.game if self._worker else None

    @property
    def is_connected(self) -> bool:
        return self._worker is not None and self._worker.connected

    # ── Session ─────────────────────────────────────────────────────────────

    def validate_session(self, token: str) -> bool:
        return self._session_token is not None and self._session_token == token

    def new_session(self) -> str:
        self._session_token = secrets.token_hex(32)
        return self._session_token

    def clear_session(self) -> None:
        self._session_token = None

    def get_waves(self) -> dict[int, list[MonsterGroup]]:
        if self._worker:
            return self._worker.waves
        return {}

    async def refresh_waves(self) -> dict[int, list[MonsterGroup]]:
        """Re-fetch wave data using existing game client."""
        if not self._worker or not self._worker.connected:
            return {}
        w = self._worker
        waves: dict[int, list[MonsterGroup]] = {}
        for wn in [1, 2]:
            w.state.log(f"Refreshing Wave {wn}...")
            try:
                grouped = await w.game.fetch_wave_grouped(wn)
                waves[wn] = grouped
                total = sum(g.count for g in grouped)
                w.state.log(f"  {len(grouped)} types, {total} monsters alive")
            except Exception as e:
                w.state.log(f"  Failed: {e}")
                waves[wn] = []
        w.waves = waves
        return waves

    # ── Connect ───────────────────────────────────────────────────────────

    async def connect(self, email: str, password: str) -> tuple[bool, dict[int, list[MonsterGroup]]]:
        """Login and fetch wave data. Returns (success, waves_grouped)."""
        # If already connected with same email, reuse existing worker
        if (self._worker and self._worker.connected
                and self._worker.email == email):
            self._worker.state.log("Session restored")
            return True, self._worker.waves

        # Different account or first time — full cleanup + login
        if self._worker:
            self._worker.state.stop()
            self._worker.pvp_state.stop()
            self._worker.stat_state.stop()
            self._worker.quest_state.stop()
            if self._worker.task:
                self._worker.task.cancel()
            if self._worker.pvp_task:
                self._worker.pvp_task.cancel()
            if self._worker.pvp_auto_task:
                self._worker.pvp_auto_task.cancel()
            if self._worker.stat_task:
                self._worker.stat_task.cancel()
            if self._worker.quest_task:
                self._worker.quest_task.cancel()
            await self._worker.game.close()

        game = GameClient()
        w = AccountWorker(game=game, email=email)
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

        # Start background hourly PvP check
        w.pvp_auto_task = asyncio.create_task(self._pvp_auto_loop())
        w.state.log("Ready! (Auto-PvP check every 1h)")
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

    async def _pvp_auto_loop(self) -> None:
        """Background loop: check for PvP tokens every hour and auto-fight."""
        w = self._worker
        if not w:
            return
        while w.connected:
            # Wait 1 hour (in 1s ticks for responsive shutdown)
            for _ in range(PVP_CHECK_INTERVAL):
                if not w or not w.connected:
                    return
                await asyncio.sleep(1)

            if not w or not w.connected:
                return

            # Skip if PvP is already running
            if self.is_pvp_running:
                logger.info("[Auto-PvP] PvP already running, skipping check")
                continue

            # Check for tokens
            try:
                tokens = await w.game.fetch_pvp_tokens()
                logger.info(f"[Auto-PvP] Hourly check — {tokens} solo tokens")
            except Exception as e:
                logger.warning(f"[Auto-PvP] Failed to check tokens: {e}")
                continue

            if tokens > 0:
                logger.info(f"[Auto-PvP] {tokens} tokens found, starting PvP")
                w.state.log(f"[Auto-PvP] {tokens} tokens detected — auto-starting PvP")
                await self.start_pvp()

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

    # ── Stat Allocator ─────────────────────────────────────────────────────

    @property
    def is_stat_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.stat_task and self._worker.stat_task.done():
            self._worker.stat_state.running = False
            return False
        return self._worker.stat_state.running

    async def start_stat_allocator(self, target_stat: str) -> bool:
        """Start the stat allocator with a target stat (attack/defense/stamina)."""
        if not self._worker or not self._worker.connected:
            return False
        if self._worker.stat_task and not self._worker.stat_task.done():
            return False
        if target_stat not in ("attack", "defense", "stamina"):
            return False

        w = self._worker
        w.stat_state = StatAllocatorState()
        w.stat_state.target_stat = target_stat
        w.stat_state.running = True

        w.stat_task = asyncio.create_task(stat_allocator_worker(w.game, w.stat_state))
        return True

    def stop_stat_allocator(self) -> None:
        """Signal the stat allocator to stop."""
        if self._worker and self._worker.stat_state.running:
            self._worker.stat_state.stop()
            self._worker.stat_state.log("[Stats] Stopping...")

    def get_stat_state(self) -> StatAllocatorState | None:
        if self._worker:
            return self._worker.stat_state
        return None

    def get_stat_stats(self) -> dict:
        if not self._worker:
            return {"target": "", "allocated": 0, "unspent": 0, "attack": 0, "defense": 0, "stamina": 0}
        s = self._worker.stat_state
        return {
            "target": s.target_stat,
            "allocated": s.total_allocated,
            "unspent": s.unspent,
            "attack": s.current_attack,
            "defense": s.current_defense,
            "stamina": s.current_stamina,
        }

    # ── Quest Runner ─────────────────────────────────────────────────────────

    @property
    def is_quest_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.quest_task and self._worker.quest_task.done():
            self._worker.quest_state.running = False
            return False
        return self._worker.quest_state.running

    async def start_quest_runner(self, targets: list[TargetConfig] | None = None) -> bool:
        """Start the quest runner. Mutually exclusive with wave farmer."""
        if not self._worker or not self._worker.connected:
            return False
        # Block if wave farmer is running
        if self.is_running:
            return False
        if self._worker.quest_task and not self._worker.quest_task.done():
            return False

        w = self._worker
        w.quest_state = QuestState()
        w.quest_state.running = True
        w.limiter.reset()

        from veyra.engine.loot_database import loot_db
        w.quest_task = asyncio.create_task(
            quest_worker(w.game, w.quest_state, loot_db, targets or [], w.limiter)
        )
        return True

    def stop_quest_runner(self) -> None:
        """Signal the quest runner to stop."""
        if self._worker and self._worker.quest_state.running:
            self._worker.quest_state.stop()
            self._worker.quest_state.log("[Quest] Stopping...")

    def get_quest_state(self) -> QuestState | None:
        if self._worker:
            return self._worker.quest_state
        return None

    def get_quest_stats(self) -> dict:
        if not self._worker:
            return {"completed": 0, "current": "", "progress": "", "fallback": False}
        s = self._worker.quest_state
        return {
            "completed": s.quests_completed,
            "current": s.current_quest,
            "progress": s.current_progress,
            "fallback": s.farming_fallback,
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
            self._worker.stat_state.stop()
            self._worker.quest_state.stop()
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
            if self._worker.pvp_auto_task:
                self._worker.pvp_auto_task.cancel()
                try:
                    await self._worker.pvp_auto_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._worker.stat_task:
                self._worker.stat_task.cancel()
                try:
                    await self._worker.stat_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._worker.quest_task:
                self._worker.quest_task.cancel()
                try:
                    await self._worker.quest_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._worker.game.close()
            self._worker = None


# Singleton for the app
manager = AccountManager()
