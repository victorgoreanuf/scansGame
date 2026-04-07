"""Local loot database — caches monster drop tables as JSON.

Scrapes battle pages once per monster type and stores results locally
so we never re-fetch the same data from the game server.

Uses a dedicated dev account (from VEYRA_DEV_EMAIL / VEYRA_DEV_PASSWORD)
for server-side scraping — no user auth needed.

Query patterns:
  - What loot does monster X drop?
  - Which monsters drop item Y?
  - What monsters drop items requiring <= N damage?
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path

from veyra.game.client import GameClient
from veyra.game.types import LootItem, MonsterLoot

logger = logging.getLogger(__name__)

LOOT_DB_FILE = Path("loot_db.json")


class LootDatabase:
    """JSON-backed loot cache with its own game client for scraping."""

    def __init__(self, path: Path = LOOT_DB_FILE):
        self._path = path
        self._data: dict[str, MonsterLoot] = {}  # keyed by monster_name (lowercase)
        self._client: GameClient | None = None
        self._logged_in: bool = False
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            for key, entry in raw.items():
                items = [LootItem(**it) for it in entry.get("items", [])]
                self._data[key] = MonsterLoot(
                    monster_name=entry["monster_name"],
                    items=items,
                    scraped_from_id=entry.get("scraped_from_id", ""),
                )
        except Exception as e:
            logger.warning("Failed to load loot DB: %s", e)

    def _save(self) -> None:
        out: dict[str, dict] = {}
        for key, ml in self._data.items():
            out[key] = {
                "monster_name": ml.monster_name,
                "scraped_from_id": ml.scraped_from_id,
                "items": [asdict(it) for it in ml.items],
            }
        self._path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    # ── Internal client ──────────────────────────────────────────────────────

    async def _ensure_client(self) -> GameClient:
        """Get or create a logged-in dev game client for scraping."""
        if self._client and self._logged_in:
            return self._client

        from veyra.config import settings

        if not settings.dev_email or not settings.dev_password:
            raise RuntimeError(
                "Dev account not configured. "
                "Set VEYRA_DEV_EMAIL and VEYRA_DEV_PASSWORD in .env"
            )

        self._client = GameClient()
        ok = await self._client.login(settings.dev_email, settings.dev_password)
        if not ok:
            raise RuntimeError("Dev account login failed")

        self._logged_in = True
        logger.info("Dev client logged in for loot scraping")
        return self._client

    # ── Scraping ─────────────────────────────────────────────────────────────

    async def scrape_monster(self, monster_id: str) -> MonsterLoot | None:
        """Scrape loot from a single monster's battle page and cache it."""
        client = await self._ensure_client()
        try:
            name, items = await client.fetch_monster_loot(monster_id)
        except Exception as e:
            logger.error("Failed to scrape loot for monster %s: %s", monster_id, e)
            self._logged_in = False  # force re-login on next attempt
            return None

        if not name:
            logger.warning("Could not determine monster name for id=%s", monster_id)
            return None

        ml = MonsterLoot(monster_name=name, items=items, scraped_from_id=monster_id)
        self._data[name.lower()] = ml
        self._save()
        logger.info("Scraped loot for %s: %d items", name, len(items))
        return ml

    async def scrape_wave(self, wave: int) -> list[MonsterLoot]:
        """Scrape loot for all monster types on a wave (skips already-cached)."""
        client = await self._ensure_client()
        monsters = await client.fetch_wave(wave)
        results: list[MonsterLoot] = []
        seen_names: set[str] = set()

        for m in monsters:
            key = m.name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)

            if key in self._data:
                logger.info("Already cached: %s", m.name)
                results.append(self._data[key])
                continue

            ml = await self.scrape_monster(m.id)
            if ml:
                results.append(ml)

        return results

    async def scrape_all_waves(self) -> list[MonsterLoot]:
        """Scrape loot for every monster type across all waves."""
        all_results: list[MonsterLoot] = []
        for wave in (1, 2, 3, 4):
            try:
                results = await self.scrape_wave(wave)
                all_results.extend(results)
            except Exception as e:
                logger.error("Failed to scrape wave %d: %s", wave, e)
        return all_results

    # ── Queries ──────────────────────────────────────────────────────────────

    def get_monster_loot(self, monster_name: str) -> MonsterLoot | None:
        """Get loot table for a specific monster (case-insensitive)."""
        return self._data.get(monster_name.lower())

    def find_item(self, item_name: str) -> list[dict]:
        """Find which monsters drop a specific item.

        Returns list of {monster_name, item} dicts.
        """
        query = item_name.lower()
        results = []
        for ml in self._data.values():
            for item in ml.items:
                if query in item.name.lower():
                    results.append({
                        "monster_name": ml.monster_name,
                        "item": asdict(item),
                    })
        return results

    def list_all(self) -> list[dict]:
        """Return all cached monster loot data."""
        return [
            {
                "monster_name": ml.monster_name,
                "scraped_from_id": ml.scraped_from_id,
                "items": [asdict(it) for it in ml.items],
            }
            for ml in self._data.values()
        ]

    def list_monsters(self) -> list[str]:
        """Return all cached monster names."""
        return [ml.monster_name for ml in self._data.values()]

    @property
    def count(self) -> int:
        return len(self._data)


# Singleton instance
loot_db = LootDatabase()
