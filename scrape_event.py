"""One-off event / blacksmith / collections scraper.

Usage:
  python scrape_event.py event [event_id] [wave]   (default: 8, 101)
  python scrape_event.py raw <path>                 dump raw HTML to debug_<slug>.html
  python scrape_event.py plan [event_id]            build event_plan.json

Logs in with the dev account.
"""

import asyncio
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from veyra.config import settings
from veyra.game.client import GameClient
from veyra.game.endpoints import BASE_URL, HEADERS
from veyra.game.parser import parse_monsters


async def _client() -> GameClient:
    if not settings.dev_email or not settings.dev_password:
        raise SystemExit("Set VEYRA_DEV_EMAIL / VEYRA_DEV_PASSWORD in .env")
    c = GameClient()
    print(f"Logging in as {settings.dev_email}...")
    if not await c.login(settings.dev_email, settings.dev_password):
        raise SystemExit("Dev login failed")
    return c


async def scrape_event(event_id: int, wave: int) -> None:
    client = await _client()
    url = f"{BASE_URL}/active_wave.php?event={event_id}&wave={wave}"
    print(f"Fetching {url}")
    resp = await client.client.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    monsters = parse_monsters(resp.text)
    unique: dict[str, str] = {}
    for m in monsters:
        if m.name.lower() not in unique:
            unique[m.name.lower()] = m.id
    print(f"Found {len(monsters)} cards, {len(unique)} unique types")

    out: dict[str, dict] = {}
    for key, mid in unique.items():
        try:
            name, items = await client.fetch_monster_loot(mid)
        except Exception as e:
            print(f"  ! {key}: {e}")
            continue
        if not name:
            continue
        out[name.lower()] = {
            "monster_name": name,
            "scraped_from_id": mid,
            "items": [asdict(it) for it in items],
        }
        print(f"  + {name}: {len(items)} items")

    out_path = Path(f"loot_db_event_{event_id}_w{wave}.json")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {out_path}")
    await client.close()


async def dump_raw(path: str) -> None:
    client = await _client()
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    print(f"Fetching {url}")
    resp = await client.client.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    slug = re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "page"
    out = Path(f"debug_{slug}.html")
    out.write_text(resp.text)
    print(f"Wrote {out} ({len(resp.text)} chars)")
    await client.close()


def _norm_img(src: str) -> str:
    if not src:
        return ""
    if src.startswith("http"):
        return src
    return f"{BASE_URL}/{src.lstrip('/')}"


def parse_collections(html: str) -> list[dict]:
    """Parse /collections.php into a list of collection definitions.

    Each collection card:
      <div class="card" data-col-id="18">
        <div class="title">Vaelith's Final Testament</div>
        <div class="req-list">
          <div class="req-item">
            <img class="req-img" src="...">
            <div style="font-weight:600;">Item Name</div>
            <div class="muted">Need: N · You have: <span class="no">X</span></div>
          </div>
          ...
        </div>
        <div class="reward">Reward: +100 <strong>attack</strong></div>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select(".card[data-col-id]"):
        title_el = card.select_one(".title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        col_id = int(card.get("data-col-id", "0"))

        reqs: list[dict] = []
        for req in card.select(".req-item"):
            name_el = req.select_one("div[style*='font-weight:600']") or req.select_one("div > div")
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            muted = req.select_one(".muted")
            need = have = 0
            if muted:
                text = muted.get_text(" ", strip=True)
                nm = re.search(r"Need:\s*([\d,]+)", text)
                if nm:
                    need = int(nm.group(1).replace(",", ""))
                have_el = muted.select_one("span")
                if have_el:
                    try:
                        have = int(have_el.get_text(strip=True).replace(",", ""))
                    except ValueError:
                        have = 0
            img_el = req.select_one("img.req-img")
            image = _norm_img(img_el.get("src", "")) if img_el else ""
            reqs.append({"name": name, "need": need, "have": have, "image": image})

        reward = ""
        reward_el = card.select_one(".reward")
        if reward_el:
            reward = reward_el.get_text(" ", strip=True).replace("Reward:", "").strip()

        out.append({
            "id": col_id,
            "name": title,
            "reward": reward,
            "requires": reqs,
        })
    return out


def parse_blacksmith(html: str) -> list[dict]:
    """Parse /blacksmith.php recipe cards.

    Each card:
      <div class="card ...">
        <img class="result-img" src="..."/>
        <button class="info-btn" data-name="..." data-attack="..." data-defense="..." data-desc="..." data-power="...">
        <div class="reqs">
          <div class="req" title="Ingredient Name">
            <img src="..."/>
            <div class="qty (ok|bad)">X/Y</div>
          </div>
        </div>
        <input name="recipe_id" value="67"/>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    grid = soup.select_one(".recipe-grid")
    cards = grid.select(".card") if grid else soup.select(".recipe-grid .card")
    for card in cards:
        info = card.select_one(".info-btn")
        name = info.get("data-name", "") if info else ""
        if not info or not name:
            name_el = card.select_one(".result-name")
            if name_el:
                name = name_el.get_text(strip=True)
        if not name:
            continue

        recipe_input = card.select_one("input[name='recipe_id']")
        recipe_id = int(recipe_input.get("value", "0")) if recipe_input else 0

        img_el = card.select_one("img.result-img")
        output_image = _norm_img(img_el.get("src", "")) if img_el else ""

        attack = defense = 0
        desc = power = ""
        if info:
            try:
                attack = int(info.get("data-attack", "0") or 0)
                defense = int(info.get("data-defense", "0") or 0)
            except ValueError:
                pass
            desc = info.get("data-desc", "")
            power = info.get("data-power", "")

        ingredients: list[dict] = []
        for req in card.select(".reqs > .req"):
            ing_name = req.get("title", "").strip()
            if not ing_name:
                alt = req.select_one("img")
                ing_name = alt.get("alt", "").strip() if alt else ""
            qty_el = req.select_one(".qty")
            need = have = 0
            if qty_el:
                m = re.match(r"([\d,]+)\s*/\s*([\d,]+)", qty_el.get_text(strip=True))
                if m:
                    have = int(m.group(1).replace(",", ""))
                    need = int(m.group(2).replace(",", ""))
            img = req.select_one("img")
            image = _norm_img(img.get("src", "")) if img else ""
            ingredients.append({
                "name": ing_name,
                "need": need,
                "have": have,
                "image": image,
            })

        out.append({
            "recipe_id": recipe_id,
            "output_name": name,
            "output_image": output_image,
            "attack": attack,
            "defense": defense,
            "description": desc,
            "power": power,
            "ingredients": ingredients,
        })
    return out


def best_sources_for(item_name: str, loot_db: dict) -> list[dict]:
    """Return monsters that drop `item_name`, sorted by lowest dmg_required first."""
    target = item_name.lower()
    sources: list[dict] = []
    for entry in loot_db.values():
        for it in entry.get("items", []):
            if it.get("name", "").lower() == target:
                sources.append({
                    "monster": entry["monster_name"],
                    "monster_id": entry["scraped_from_id"],
                    "drop_rate": it.get("drop_rate", ""),
                    "dmg_required": it.get("dmg_required", 0),
                    "rarity": it.get("rarity", ""),
                })
                break
    sources.sort(key=lambda s: (s["dmg_required"] or 10**12))
    return sources


async def build_plan(event_id: int) -> None:
    client = await _client()
    print("Fetching /collections.php ...")
    col_html = (await client.client.get(f"{BASE_URL}/collections.php", headers=HEADERS, timeout=30)).text
    print("Fetching /blacksmith.php ...")
    bs_html = (await client.client.get(f"{BASE_URL}/blacksmith.php", headers=HEADERS, timeout=30)).text
    await client.close()

    collections = parse_collections(col_html)
    recipes = parse_blacksmith(bs_html)

    # Load event loot DB (if present) to compute best sources
    loot_db: dict = {}
    loot_file = Path(f"loot_db_event_{event_id}_w101.json")
    if loot_file.exists():
        loot_db = json.loads(loot_file.read_text())

    # Build best-source map across every ingredient referenced (collections + recipes)
    referenced: set[str] = set()
    for c in collections:
        for r in c["requires"]:
            referenced.add(r["name"])
    for r in recipes:
        for ing in r["ingredients"]:
            referenced.add(ing["name"])

    best_sources: dict[str, list[dict]] = {}
    for name in sorted(referenced):
        srcs = best_sources_for(name, loot_db)
        if srcs:
            best_sources[name] = srcs

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "collections": collections,
        "recipes": recipes,
        "best_sources": best_sources,
    }
    out_path = Path(f"event_{event_id}_plan.json")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {out_path}: {len(collections)} collections, {len(recipes)} recipes, "
          f"{len(best_sources)} items mapped to monsters")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "event"
    if mode == "event":
        event_id = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        wave = int(sys.argv[3]) if len(sys.argv) > 3 else 101
        asyncio.run(scrape_event(event_id, wave))
    elif mode == "raw":
        asyncio.run(dump_raw(sys.argv[2]))
    elif mode == "plan":
        event_id = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        asyncio.run(build_plan(event_id))
    else:
        raise SystemExit(f"Unknown mode: {mode}")
