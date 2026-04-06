"""Async game client — one instance per account, wraps all game HTTP calls."""

import httpx

from veyra.game.auth import do_login
from veyra.game.endpoints import (
    BASE_URL,
    BATTLE_URL,
    CHAPTER_URL,
    DAMAGE_URL,
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
    USE_ITEM_URL,
    WAVE_MAP,
)
from veyra.game.parser import (
    extract_user_id,
    group_monsters,
    parse_chapter_id,
    parse_chapter_list,
    parse_damage_response,
    parse_dead_monsters,
    parse_exp_per_dmg,
    parse_farmed_today,
    parse_manga_links,
    parse_monsters,
    parse_player_stats,
    parse_pvp_solo_tokens,
    parse_stamina_potions,
    parse_unclaimed_kills,
)
from veyra.game.types import AttackResult, DeadMonster, Monster, MonsterGroup, PlayerStats, StaminaPotion


class GameClient:
    """Async HTTP wrapper for all Demonic Scans game endpoints."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=15, follow_redirects=True
        )
        self.user_id: str = ""

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
        data = resp.json()
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
