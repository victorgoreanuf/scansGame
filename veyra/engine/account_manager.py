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
from veyra.engine.team_pvp_fighter import TeamPvPState, team_pvp_worker
from veyra.engine.stat_allocator import StatAllocatorState, StatGoal, stat_allocator_worker
from veyra.engine.quest_runner import QuestState, quest_worker
from veyra.engine.collection_farmer import (
    COLLECTION_PLANS,
    CollectionState,
    collection_worker,
)
from veyra.engine.achievement_farmer import (
    AchievementState,
    achievement_worker,
)


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
    # Team PvP (party)
    team_pvp_state: TeamPvPState = field(default_factory=TeamPvPState)
    team_pvp_task: asyncio.Task | None = None
    # Stat allocator
    stat_state: StatAllocatorState = field(default_factory=StatAllocatorState)
    stat_task: asyncio.Task | None = None
    # Quest runner
    quest_state: QuestState = field(default_factory=QuestState)
    quest_task: asyncio.Task | None = None
    # Collection farmer
    collection_state: CollectionState = field(default_factory=CollectionState)
    collection_task: asyncio.Task | None = None
    # Achievement farmer
    achievement_state: AchievementState = field(default_factory=AchievementState)
    achievement_task: asyncio.Task | None = None


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
        for wn in [1, 2, 3]:
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
            self._worker.team_pvp_state.stop()
            self._worker.stat_state.stop()
            self._worker.quest_state.stop()
            self._worker.collection_state.stop()
            self._worker.achievement_state.stop()
            if self._worker.task:
                self._worker.task.cancel()
            if self._worker.pvp_task:
                self._worker.pvp_task.cancel()
            if self._worker.team_pvp_task:
                self._worker.team_pvp_task.cancel()
            if self._worker.pvp_auto_task:
                self._worker.pvp_auto_task.cancel()
            if self._worker.stat_task:
                self._worker.stat_task.cancel()
            if self._worker.quest_task:
                self._worker.quest_task.cancel()
            if self._worker.collection_task:
                self._worker.collection_task.cancel()
            if self._worker.achievement_task:
                self._worker.achievement_task.cancel()
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
        for wn in [1, 2, 3]:
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
        if self.is_collection_running:
            return False
        if self.is_achievement_running:
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
        """Background loop: check for solo + party tokens every hour and auto-fight."""
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

            # Solo
            if self.is_pvp_running:
                logger.info("[Auto-PvP] Solo PvP already running, skipping solo check")
            else:
                try:
                    tokens = await w.game.fetch_pvp_tokens()
                    logger.info(f"[Auto-PvP] Hourly check — {tokens} solo tokens")
                except Exception as e:
                    logger.warning(f"[Auto-PvP] Failed to check solo tokens: {e}")
                    tokens = 0

                if tokens > 0:
                    logger.info(f"[Auto-PvP] {tokens} solo tokens found, starting PvP")
                    w.state.log(f"[Auto-PvP] {tokens} solo tokens detected — auto-starting PvP")
                    await self.start_pvp()

            # Team (only if user is leader and has party tokens)
            if self.is_team_pvp_running:
                logger.info("[Auto-PvP] Team PvP already running, skipping team check")
                continue
            try:
                status = await w.game.fetch_pvp_party_status()
            except Exception as e:
                logger.warning(f"[Auto-PvP] Failed to check party status: {e}")
                continue

            if not status.get("in_party"):
                continue
            if not status.get("is_leader"):
                logger.info("[Auto-PvP] In party but not leader — skipping team check")
                continue
            party_tokens = int(status.get("tokens", 0))
            logger.info(f"[Auto-PvP] Hourly check — {party_tokens} party tokens (leader)")
            if party_tokens > 0:
                w.state.log(
                    f"[Auto-PvP] {party_tokens} party tokens detected — auto-starting team PvP"
                )
                await self.start_team_pvp()

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

    # ── Team PvP (party) ───────────────────────────────────────────────────

    @property
    def is_team_pvp_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.team_pvp_task and self._worker.team_pvp_task.done():
            self._worker.team_pvp_state.running = False
            return False
        return self._worker.team_pvp_state.running

    async def start_team_pvp(self) -> tuple[bool, str]:
        """Start the team-PvP auto-fight worker. Only valid if user is the party leader."""
        if not self._worker or not self._worker.connected:
            return False, "Not connected"
        if self._worker.team_pvp_task and not self._worker.team_pvp_task.done():
            return False, "Team PvP already running"

        w = self._worker
        w.team_pvp_state = TeamPvPState()
        w.team_pvp_state.running = True
        w.team_pvp_task = asyncio.create_task(team_pvp_worker(w.game, w.team_pvp_state))
        return True, ""

    def stop_team_pvp(self) -> None:
        if self._worker and self._worker.team_pvp_state.running:
            self._worker.team_pvp_state.stop()
            self._worker.team_pvp_state.log("Stopping Team PvP...")

    def get_team_pvp_state(self) -> TeamPvPState | None:
        if self._worker:
            return self._worker.team_pvp_state
        return None

    def get_team_pvp_stats(self) -> dict:
        if not self._worker:
            return {
                "matches": 0, "wins": 0, "losses": 0,
                "tokens": 0, "tokens_max": 0,
                "in_party": False, "is_leader": False, "party_name": "",
            }
        s = self._worker.team_pvp_state
        return {
            "matches": s.matches_played,
            "wins": s.wins,
            "losses": s.losses,
            "tokens": s.tokens_remaining,
            "tokens_max": s.tokens_max,
            "in_party": s.in_party,
            "is_leader": s.is_leader,
            "party_name": s.party_name,
        }

    async def fetch_party_status(self) -> dict:
        """One-shot party status fetch for the UI (so it can show the card only when relevant)."""
        if not self._worker or not self._worker.connected:
            return {"in_party": False, "is_leader": False, "tokens": 0, "tokens_max": 0, "party_name": ""}
        try:
            return await self._worker.game.fetch_pvp_party_status()
        except Exception as e:
            logger.warning(f"fetch_party_status failed: {e}")
            return {"in_party": False, "is_leader": False, "tokens": 0, "tokens_max": 0, "party_name": ""}

    # ── Stat Allocator ─────────────────────────────────────────────────────

    @property
    def is_stat_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.stat_task and self._worker.stat_task.done():
            self._worker.stat_state.running = False
            return False
        return self._worker.stat_state.running

    async def start_stat_allocator(self, goals: list[StatGoal], default_stat: str = "stamina") -> bool:
        """Start the stat allocator with priority goals and a default stat."""
        if not self._worker or not self._worker.connected:
            return False
        if self._worker.stat_task and not self._worker.stat_task.done():
            return False

        w = self._worker
        w.stat_state = StatAllocatorState()
        w.stat_state.goals = goals
        w.stat_state.default_stat = default_stat
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
            return {"goals": [], "default_stat": "", "allocated": 0, "unspent": 0, "attack": 0, "defense": 0, "stamina": 0, "active_goal_index": 0}
        s = self._worker.stat_state
        return {
            "goals": [{"stat": g.stat, "target": g.target} for g in s.goals],
            "default_stat": s.default_stat,
            "allocated": s.total_allocated,
            "unspent": s.unspent,
            "attack": s.current_attack,
            "defense": s.current_defense,
            "stamina": s.current_stamina,
            "active_goal_index": s.active_goal_index,
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
        # Block if wave / collection / achievement farmer is running
        if self.is_running or self.is_collection_running or self.is_achievement_running:
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

    # ── Collection Farmer ─────────────────────────────────────────────────────

    @property
    def is_collection_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.collection_task and self._worker.collection_task.done():
            self._worker.collection_state.running = False
            return False
        return self._worker.collection_state.running

    async def start_collection(self, collection_id: int, stamina_label: str = "10 Stamina") -> tuple[bool, str]:
        """Start farming ingredients for a collection. Mutually exclusive with wave farmer + quests."""
        if not self._worker or not self._worker.connected:
            return False, "Not connected"
        if collection_id not in COLLECTION_PLANS:
            return False, f"Unknown collection id {collection_id}"
        if self.is_running:
            return False, "Wave farmer is running — stop it first"
        if self.is_quest_running:
            return False, "Quest runner is running — stop it first"
        if self.is_achievement_running:
            return False, "Achievement farmer is running — stop it first"
        if self._worker.collection_task and not self._worker.collection_task.done():
            return False, "Collection farmer is already running"

        w = self._worker
        w.collection_state = CollectionState()
        w.collection_state.collection_id = collection_id
        w.collection_state.collection_name = COLLECTION_PLANS[collection_id]["name"]
        w.collection_state.stamina_label = stamina_label
        w.collection_state.running = True
        w.collection_state.stats.started_at = time.time()
        w.limiter.reset()

        w.collection_task = asyncio.create_task(
            collection_worker(w.game, w.collection_state, w.limiter)
        )
        return True, ""

    def stop_collection(self) -> None:
        if self._worker and self._worker.collection_state.running:
            self._worker.collection_state.stop()
            self._worker.collection_state.log("Stopping...")

    def get_collection_state(self) -> CollectionState | None:
        if self._worker:
            return self._worker.collection_state
        return None

    def get_collection_status(self) -> dict:
        if not self._worker:
            return {"running": False, "collection_id": 0, "collection_name": "",
                    "current_item": "", "progress": {}, "stats": {}}
        s = self._worker.collection_state
        stats = s.stats
        return {
            "running": self.is_collection_running,
            "collection_id": s.collection_id,
            "collection_name": s.collection_name,
            "current_item": s.current_item,
            "progress": s.progress,
            "stats": {
                "killed": stats.killed,
                "damage": stats.damage,
                "stamina_spent": stats.stamina_spent,
                "looted": stats.looted,
                "monsters_attacked": stats.monsters_attacked,
                "started_at": stats.started_at,
            },
        }

    # ── Achievement Farmer ────────────────────────────────────────────────────

    @property
    def is_achievement_running(self) -> bool:
        if self._worker is None:
            return False
        if self._worker.achievement_task and self._worker.achievement_task.done():
            self._worker.achievement_state.running = False
            return False
        return self._worker.achievement_state.running

    async def start_achievements(
        self,
        wave: int = 101,
        stamina_label: str = "10 Stamina",
    ) -> tuple[bool, str]:
        """Start the achievement farmer. Mutually exclusive with the other workers."""
        if not self._worker or not self._worker.connected:
            return False, "Not connected"
        if self.is_running:
            return False, "Wave farmer is running — stop it first"
        if self.is_quest_running:
            return False, "Quest runner is running — stop it first"
        if self.is_collection_running:
            return False, "Collection farmer is running — stop it first"
        if self._worker.achievement_task and not self._worker.achievement_task.done():
            return False, "Achievement farmer is already running"

        w = self._worker
        w.achievement_state = AchievementState()
        w.achievement_state.wave = wave
        w.achievement_state.stamina_label = stamina_label
        w.achievement_state.running = True
        w.achievement_state.stats.started_at = time.time()
        w.limiter.reset()

        w.achievement_task = asyncio.create_task(
            achievement_worker(w.game, w.achievement_state, w.limiter)
        )
        return True, ""

    def stop_achievements(self) -> None:
        if self._worker and self._worker.achievement_state.running:
            self._worker.achievement_state.stop()
            self._worker.achievement_state.log("Stopping...")

    def get_achievement_state(self) -> AchievementState | None:
        if self._worker:
            return self._worker.achievement_state
        return None

    def get_achievement_status(self) -> dict:
        if not self._worker:
            return {
                "running": False, "wave": 0, "stamina_label": "",
                "current_monster": "", "wave_monsters": [],
                "achievements": [], "active": [], "stats": {},
            }
        s = self._worker.achievement_state
        stats = s.stats
        return {
            "running": self.is_achievement_running,
            "wave": s.wave,
            "stamina_label": s.stamina_label,
            "current_monster": s.current_monster,
            "wave_monsters": s.wave_monsters,
            "achievements": s.achievements,
            "active": s.active,
            "stats": {
                "killed": stats.killed,
                "damage": stats.damage,
                "stamina_spent": stats.stamina_spent,
                "looted": stats.looted,
                "monsters_attacked": stats.monsters_attacked,
                "started_at": stats.started_at,
            },
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
            self._worker.team_pvp_state.stop()
            self._worker.stat_state.stop()
            self._worker.quest_state.stop()
            self._worker.collection_state.stop()
            self._worker.achievement_state.stop()
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
            if self._worker.team_pvp_task:
                self._worker.team_pvp_task.cancel()
                try:
                    await self._worker.team_pvp_task
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
            if self._worker.collection_task:
                self._worker.collection_task.cancel()
                try:
                    await self._worker.collection_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._worker.achievement_task:
                self._worker.achievement_task.cancel()
                try:
                    await self._worker.achievement_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._worker.game.close()
            self._worker = None


# Singleton for the app
manager = AccountManager()
