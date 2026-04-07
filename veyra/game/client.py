"""Async game client — one instance per account, wraps all game HTTP calls."""

import asyncio
import logging
from typing import Callable

import httpx

logger = logging.getLogger("veyra.client")

from veyra.game.auth import do_login
from veyra.game.endpoints import (
    ALLOCATE_STAT_URL,
    BASE_URL,
    BATTLE_URL,
    CHAPTER_URL,
    DAMAGE_URL,
    GUILD_ACCEPT_URL,
    GUILD_FINISH_URL,
    GUILD_GIVEUP_URL,
    GUILD_URL,
    HEADERS,
    ATTACK_EXTRA_HEADERS,
    INVENTORY_URL,
    JOIN_URL,
    LAST_UPDATES_URL,
    LOOT_URL,
    PVP_BATTLE_ACTION_URL,
    PVP_BATTLE_STATE_URL,
    PVP_BATTLE_URL,
    PVP_MATCHMAKE_URL,
    PVP_URL,
    REACT_URL,
    STATS_URL,
    USE_ITEM_URL,
    WAVE_MAP,
)
from veyra.game.parser import (
    extract_user_id,
    group_monsters,
    parse_active_quest,
    parse_chapter_id,
    parse_chapter_list,
    parse_character_stats,
    parse_class_skills,
    parse_damage_response,
    parse_dead_monsters,
    parse_exp_per_dmg,
    parse_farmed_today,
    parse_manga_links,
    parse_monster_loot,
    parse_monsters,
    parse_player_stats,
    parse_pvp_solo_tokens,
    parse_quest_board,
    parse_stamina_potions,
    parse_unclaimed_kills,
)
from veyra.game.types import AttackResult, CharacterStats, DeadMonster, LootItem, Monster, MonsterGroup, PlayerStats, StaminaPotion


SITE_DOWN_THRESHOLD = 3        # consecutive failures before declaring site down
SITE_CHECK_INTERVAL = 60       # seconds between recovery checks


class GameClient:
    """Async HTTP wrapper for all Demonic Scans game endpoints."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=15, follow_redirects=True
        )
        self.user_id: str = ""
        # Site health tracking
        self._consecutive_net_failures: int = 0
        self._site_down: bool = False

    @property
    def is_site_down(self) -> bool:
        return self._site_down

    def record_net_success(self) -> None:
        """Call after any successful HTTP response (even game-level errors)."""
        self._consecutive_net_failures = 0
        self._site_down = False

    def record_net_failure(self) -> None:
        """Call after network-level failures (timeout, connection error, 5xx)."""
        self._consecutive_net_failures += 1
        if self._consecutive_net_failures >= SITE_DOWN_THRESHOLD:
            self._site_down = True

    async def wait_for_site_up(
        self,
        log_fn: Callable[[str], None],
        stop_check: Callable[[], bool],
    ) -> bool:
        """Block until the site recovers or stop is signaled. Returns True if recovered."""
        log_fn("")
        log_fn(
            f"=== SITE DOWN — {self._consecutive_net_failures} consecutive failures ==="
        )
        log_fn(f"Checking every {SITE_CHECK_INTERVAL}s until site recovers...")

        while self._site_down:
            # Sleep in 1s ticks for responsive stopping
            for _ in range(SITE_CHECK_INTERVAL):
                if stop_check():
                    return False
                await asyncio.sleep(1)

            # Quick health check
            try:
                resp = await self._client.get(
                    BASE_URL, headers=HEADERS, timeout=10
                )
                if resp.status_code < 500:
                    self._site_down = False
                    self._consecutive_net_failures = 0
                    log_fn("")
                    log_fn("=== SITE IS BACK UP — resuming ===")
                    return True
                log_fn(
                    f"Still down (HTTP {resp.status_code}), "
                    f"next check in {SITE_CHECK_INTERVAL}s..."
                )
            except Exception as e:
                log_fn(
                    f"Still down ({type(e).__name__}), "
                    f"next check in {SITE_CHECK_INTERVAL}s..."
                )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self, email: str, password: str) -> bool:
        ok = await do_login(self._client, email, password)
        return ok

    async def fetch_wave(self, wave: int) -> list[Monster]:
        """Fetch all alive monsters from a wave page."""
        url = WAVE_MAP.get(wave)
        if not url:
            raise ValueError(f"Invalid wave number: {wave}")
        resp = await self._client.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        self.user_id = extract_user_id(resp.text)
        return parse_monsters(resp.text)

    async def fetch_wave_grouped(self, wave: int) -> list[MonsterGroup]:
        """Fetch monsters and group by name."""
        monsters = await self.fetch_wave(wave)
        return group_monsters(monsters)

    async def fetch_wave_raw(self, wave: int) -> str:
        """Fetch wave page and return raw HTML."""
        url = WAVE_MAP.get(wave)
        if not url:
            raise ValueError(f"Invalid wave number: {wave}")
        resp = await self._client.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        self.user_id = extract_user_id(resp.text)
        return resp.text

    async def join_battle(self, monster_id: str) -> None:
        """Send join-battle request for a monster."""
        await self._client.post(
            JOIN_URL,
            data={"monster_id": monster_id, "user_id": self.user_id},
            headers=HEADERS,
            timeout=15,
        )

    async def attack(
        self, monster_id: str, skill_id: str, stamina_cost: int, prev_hp: int | None = None
    ) -> AttackResult:
        """Send a single attack and return parsed result."""
        resp = await self._client.post(
            DAMAGE_URL,
            data={
                "monster_id": monster_id,
                "skill_id": skill_id,
                "stamina_cost": str(stamina_cost),
            },
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": f"{BASE_URL}/battle.php?id={monster_id}"},
            timeout=15,
        )
        try:
            data = resp.json()
        except Exception:
            logger.error("attack: non-JSON response (HTTP %s): %s", resp.status_code, resp.text[:300])
            return AttackResult(is_success=False, damage=0, message=f"Non-JSON response: {resp.text[:100]}")
        return parse_damage_response(data, prev_hp)

    # ── Loot / EXP methods ────────────────────────────────────────────────

    async def fetch_player_stats(self, wave: int = 2) -> PlayerStats:
        """Fetch player stats (EXP, level, stamina) from a wave page header."""
        html = await self.fetch_wave_raw(wave)
        return parse_player_stats(html)

    async def fetch_dead_monsters(self, wave: int) -> list[DeadMonster]:
        """Fetch dead/lootable monsters from a wave page with dead monsters visible."""
        url = WAVE_MAP.get(wave)
        if not url:
            raise ValueError(f"Invalid wave number: {wave}")
        # Override hide_dead_monsters=0 in the cookie header directly
        # to ensure the server renders dead monster cards in the HTML
        cookie_header = "; ".join(
            f"{name}={value}" for name, value in self._client.cookies.items()
        )
        # Force hide_dead_monsters=0 regardless of what's in the jar
        if "hide_dead_monsters" in cookie_header:
            cookie_header = cookie_header.replace("hide_dead_monsters=1", "hide_dead_monsters=0")
        else:
            cookie_header += "; hide_dead_monsters=0"
        resp = await self._client.get(
            url,
            headers={**HEADERS, "Cookie": cookie_header},
            timeout=15,
        )
        resp.raise_for_status()
        return parse_dead_monsters(resp.text)

    async def fetch_unclaimed_kills(self, wave: int) -> int:
        """Get count of unclaimed kills on a wave page."""
        html = await self.fetch_wave_raw(wave)
        return parse_unclaimed_kills(html)

    async def fetch_exp_per_dmg(self, monster_id: str) -> float:
        """Fetch EXP/DMG ratio from a monster's battle page."""
        resp = await self._client.get(
            f"{BATTLE_URL}?id={monster_id}", headers=HEADERS, timeout=15
        )
        return parse_exp_per_dmg(resp.text)

    async def loot_monster(self, monster_id: str) -> dict:
        """Loot a single dead monster. Returns the server response."""
        resp = await self._client.post(
            LOOT_URL,
            data={"monster_id": monster_id, "user_id": self.user_id},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": f"{BASE_URL}/battle.php?id={monster_id}"},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            logger.warning("loot_monster(%s): non-JSON response (HTTP %s): %s", monster_id, resp.status_code, resp.text[:300])
            return {"status": "error", "message": resp.text[:200]}

    # ── Stamina potions ────────────────────────────────────────────────────

    async def fetch_stamina_potions(self) -> list[StaminaPotion]:
        """Fetch available stamina potions from inventory page."""
        html = await self.fetch_page(INVENTORY_URL)
        return parse_stamina_potions(html)

    async def use_stamina_potion(self, inv_id: str) -> bool:
        """Use a stamina potion by inventory ID. Returns True on success."""
        resp = await self._client.post(
            USE_ITEM_URL,
            data={"inv_id": inv_id},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": f"{BASE_URL}/game_dash.php"},
            timeout=15,
        )
        return 200 <= resp.status_code < 300

    # ── Manga / Reaction stamina farming ────────────────────────────────────

    async def fetch_page(self, url: str) -> str:
        """Fetch any page on the site using the authenticated session."""
        resp = await self._client.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text

    async def fetch_farmed_today(self) -> tuple[int, int]:
        """Get (farmed_today, daily_cap) from any page with the user header."""
        html = await self.fetch_wave_raw(1)
        return parse_farmed_today(html)

    async def discover_chapters(self) -> list[tuple[str, str]]:
        """Discover manga series from lastupdates, then extract chapter lists.

        Returns list of (manga_id, chapter_number) tuples across multiple series.
        """
        # 1. Get manga/chapter links from the latest updates page
        html = await self.fetch_page(LAST_UPDATES_URL)
        manga_links = parse_manga_links(html)

        # Also check if this page itself has chapter option lists
        all_chapters = parse_chapter_list(html)

        # 2. Visit manga/chapter links to discover more chapters
        visited = 0
        for link in manga_links:
            if visited >= 5:
                break
            if all_chapters and len(all_chapters) > 200:
                break
            try:
                page_html = await self.fetch_page(link)
                chapters = parse_chapter_list(page_html)
                if chapters:
                    existing = set(all_chapters)
                    for ch in chapters:
                        if ch not in existing:
                            all_chapters.append(ch)
                            existing.add(ch)
                    visited += 1
            except Exception:
                continue

        return all_chapters

    async def react_to_chapter(self, manga_id: str, chapter: str) -> tuple[bool, str]:
        """Visit a chapter page, extract its internal chapterid, POST a reaction.

        The site uses:
          POST /postreaction.php  (multipart/form-data)
          Fields: chapterid (internal DB id), reaction (1-5), useruid (from cookie)

        Returns (success, debug_info).
        """
        # 1. Visit the chapter page to get the internal chapterid
        chapter_url = f"{CHAPTER_URL}?manga={manga_id}&chapter={chapter}"
        try:
            html = await self.fetch_page(chapter_url)
        except Exception as e:
            return False, f"fetch failed: {e}"

        chapter_id = parse_chapter_id(html)
        if not chapter_id:
            return False, "no chapterid found in page"

        # 2. Get useruid from cookies
        useruid = self._client.cookies.get("useruid", "")
        if not useruid:
            # Try to find it in all cookies
            for name in self._client.cookies.jar:
                if name.name == "useruid":
                    useruid = name.value
                    break

        # 3. POST reaction as multipart/form-data (matching the browser's request)
        resp = await self._client.post(
            REACT_URL,
            data={
                "chapterid": chapter_id,
                "reaction": "1",  # 1=👍
                "useruid": useruid,
            },
            headers={**HEADERS, "Referer": f"{BASE_URL}/"},
            timeout=15,
        )
        body = resp.text[:200]
        ok = 200 <= resp.status_code < 300
        return ok, f"[{resp.status_code}] cid={chapter_id} {body}"

    # ── PvP methods ─────────────────────────────────────────────────────────

    async def fetch_pvp_tokens(self) -> int:
        """Fetch solo PvP token count from the PvP page."""
        html = await self.fetch_page(PVP_URL)
        return parse_pvp_solo_tokens(html)

    async def pvp_find_match(self, ladder: str = "solo") -> dict:
        """Queue for a PvP match. Returns the server response JSON."""
        resp = await self._client.post(
            PVP_MATCHMAKE_URL,
            data={"ladder": ladder},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": PVP_URL},
            timeout=30,
        )
        try:
            return resp.json()
        except Exception:
            logger.warning("pvp_find_match: non-JSON response (HTTP %s): %s", resp.status_code, resp.text[:300])
            return {"error": resp.text[:300]}

    async def pvp_set_auto(self, match_id: str, since_log_id: int = 0) -> dict:
        """Set solo battle to auto-play mode."""
        resp = await self._client.post(
            PVP_BATTLE_ACTION_URL,
            data={
                "match_id": match_id,
                "since_log_id": str(since_log_id),
                "action": "set_solo_control_mode",
                "control_mode": "auto",
            },
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": f"{PVP_BATTLE_URL}?match_id={match_id}"},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"error": resp.text[:300]}

    async def pvp_poll_state(self, match_id: str, since_log_id: int = 0) -> dict:
        """Poll battle state. Returns JSON with battle progress and logs."""
        resp = await self._client.get(
            f"{PVP_BATTLE_STATE_URL}?match_id={match_id}&since_log_id={since_log_id}",
            headers={**HEADERS, "Referer": f"{PVP_BATTLE_URL}?match_id={match_id}"},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"error": resp.text[:300]}

    # ── Stat allocation ──────────────────────────────────────────────────────

    async def fetch_character_stats(self) -> CharacterStats:
        """Fetch current character stats and unspent points from stats.php."""
        html = await self.fetch_page(STATS_URL)
        return parse_character_stats(html)

    async def allocate_stat(self, stat: str, amount: int) -> dict:
        """Allocate stat points. stat should be 'attack', 'defense', or 'stamina'."""
        resp = await self._client.post(
            ALLOCATE_STAT_URL,
            data={"action": "allocate", "stat": stat, "amount": str(amount)},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": STATS_URL},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "message": resp.text[:300]}

    # ── Guild / Quest methods ───────────────────────────────────────────────

    async def fetch_quest_board_raw(self) -> str:
        """Fetch the adventurers guild quest board and return raw HTML."""
        return await self.fetch_page(GUILD_URL)

    async def fetch_quest_board(self) -> list:
        """Fetch and parse quest board. Returns list of Quest objects."""
        html = await self.fetch_page(GUILD_URL)
        return parse_quest_board(html)

    async def fetch_active_quest(self):
        """Check for an active quest with progress. Returns ActiveQuest or None."""
        html = await self.fetch_page(GUILD_URL)
        return parse_active_quest(html)

    async def accept_quest(self, quest_id: int) -> dict:
        """Accept a quest from the board. Returns server response dict."""
        resp = await self._client.post(
            GUILD_ACCEPT_URL,
            data={"quest_id": str(quest_id)},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": GUILD_URL},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "message": resp.text[:300]}

    async def finish_quest(self, quest_id: int) -> dict:
        """Turn in a completed quest. Returns server response dict."""
        resp = await self._client.post(
            GUILD_FINISH_URL,
            data={"quest_id": str(quest_id)},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": GUILD_URL},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "message": resp.text[:300]}

    async def giveup_quest(self, quest_id: int) -> dict:
        """Abandon an active quest. Returns server response dict."""
        resp = await self._client.post(
            GUILD_GIVEUP_URL,
            data={"quest_id": str(quest_id)},
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": GUILD_URL},
            timeout=15,
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "message": resp.text[:300]}

    async def fetch_class_skills(self, monster_id: str) -> list[dict]:
        """Fetch class skills from a battle page (must have joined the battle).

        Returns list of dicts with id, name, mp_cost. Empty = no class.
        """
        html = await self.fetch_page(f"{BATTLE_URL}?id={monster_id}")
        return parse_class_skills(html)

    async def use_class_skill(self, monster_id: str, skill_id: str) -> AttackResult:
        """Use a class skill against a monster. Costs MP, 1 stamina."""
        resp = await self._client.post(
            DAMAGE_URL,
            data={
                "monster_id": monster_id,
                "skill_id": skill_id,
                "stamina_cost": "1",
            },
            headers={**HEADERS, **ATTACK_EXTRA_HEADERS, "Referer": f"{BASE_URL}/battle.php?id={monster_id}"},
            timeout=15,
        )
        data = resp.json()
        return parse_damage_response(data, None)

    async def fetch_monster_loot(self, monster_id: str) -> tuple[str, list[LootItem]]:
        """Fetch the battle page for a monster and parse its possible loot.

        Returns (monster_name, list_of_loot_items).
        """
        html = await self.fetch_page(f"{BATTLE_URL}?id={monster_id}")
        return parse_monster_loot(html)

    def get_cookies_json(self) -> str:
        """Serialize current cookies to JSON for persistence."""
        import json
        jar = self._client.cookies
        cookies = {name: jar.get(name, "") for name in jar}  # type: ignore[arg-type]
        return json.dumps(cookies)

    def load_cookies_json(self, cookies_json: str) -> None:
        """Load cookies from JSON string to restore a session."""
        import json
        cookies = json.loads(cookies_json)
        for name, value in cookies.items():
            self._client.cookies.set(name, value)
