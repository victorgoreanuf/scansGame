"""HTML/JSON parsing — ported from slasher_app.py:117-286."""

import logging
import re

from bs4 import BeautifulSoup, Tag

from veyra.game.endpoints import BASE_URL

logger = logging.getLogger("veyra.parser")
from veyra.game.types import (
    ActiveQuest,
    AttackResult,
    CharacterStats,
    DeadMonster,
    LootItem,
    Monster,
    MonsterGroup,
    PlayerStats,
    Quest,
    QuestObjective,
    QuestStatus,
    QuestType,
    StaminaPotion,
)


def parse_monsters(html: str) -> list[Monster]:
    """Parse monster cards from a wave page HTML. Returns list of alive monsters."""
    soup = BeautifulSoup(html, "html.parser")
    monsters: list[Monster] = []

    for card in soup.select(".monster-card"):
        if card.get("data-dead", "0") == "1":
            continue

        mid = _extract_monster_id(card)
        if not mid:
            continue

        name = _extract_monster_name(card, mid)
        current_hp = _extract_hp(card)
        your_dmg = _extract_your_damage(card)
        image = _extract_image(card)
        joined = _detect_joined(card)

        monsters.append(
            Monster(
                id=mid,
                name=name,
                current_hp=current_hp,
                your_dmg=your_dmg,
                image=image,
                joined=joined,
            )
        )

    return monsters


def extract_user_id(html: str) -> str:
    """Extract user ID from page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one('a[href*="player.php?pid="]')
    if link:
        m = re.search(r"pid=(\d+)", link["href"])
        if m:
            return m.group(1)
    inp = soup.select_one('input[name="user_id"]')
    if inp:
        return inp.get("value", "150205")
    return "150205"


def group_monsters(monsters: list[Monster]) -> list[MonsterGroup]:
    """Group monsters by name, compute stats per group."""
    groups: dict[str, MonsterGroup] = {}

    for m in monsters:
        if m.name not in groups:
            groups[m.name] = MonsterGroup(name=m.name, image=m.image)
        g = groups[m.name]
        g.count += 1
        g.ids.append(m.id)
        g.total_hp += m.current_hp
        g.max_hp = max(g.max_hp, m.current_hp)
        g.instances.append(m)
        g.total_your_dmg += m.your_dmg

    result = list(groups.values())
    for g in result:
        g.avg_hp = g.total_hp // g.count if g.count else 0
        g.joined_count = sum(1 for i in g.instances if i.joined)
        g.new_count = g.count - g.joined_count
        g.instances.sort(key=lambda x: x.current_hp, reverse=True)

    result.sort(key=lambda g: g.max_hp, reverse=True)
    return result


def parse_damage_response(data: dict, prev_hp: int | None = None) -> AttackResult:
    """Parse the JSON response from a damage.php POST."""
    msg = data.get("message", "")

    # Monster already dead
    if msg.lower() == "monster is already dead.":
        return AttackResult(status="dead", message=msg, raw=data)

    # Rate limited
    if "slow down" in msg.lower():
        return AttackResult(status="rate_limited", message=msg, raw=data)

    # Out of stamina
    if "stamina" in msg.lower():
        return AttackResult(status="stamina", message=msg, raw=data)

    # Success
    if data.get("status") == "success":
        dmg = _parse_damage_value(data, prev_hp)
        hp = -1
        if "hp" in data and isinstance(data["hp"], dict):
            try:
                hp = int(data["hp"].get("value", -1))
            except (ValueError, TypeError):
                pass
        return AttackResult(status="success", damage=dmg, monster_hp=hp, message=msg, raw=data)

    # Unknown / error
    logger.warning("Unhandled damage response: %s", data)
    return AttackResult(status="error", message=msg or "Unknown response", raw=data)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_monster_id(card: Tag) -> str | None:
    a_el = card.select_one('a[href*="battle.php?id="]')
    if a_el:
        m = re.search(r"id=(\d+)", a_el["href"])
        if m:
            return m.group(1)
    inp = card.select_one('input[name="monster_id"]')
    if inp:
        return inp["value"]
    return None


def _extract_monster_name(card: Tag, monster_id: str) -> str:
    name_el = (
        card.select_one(".card-title")
        or card.select_one(".monster-name")
        or card.select_one("h3")
        or card.select_one("h4")
        or card.select_one("b")
        or card.select_one("strong")
    )
    name = name_el.get_text(strip=True) if name_el else f"Unknown({monster_id})"
    # Strip leading emoji characters
    return re.sub(r"^[^\w]+", "", name).strip()


def _extract_hp(card: Tag) -> int:
    hp_sub = card.select_one(".card-sub")
    if hp_sub:
        nums = re.findall(r"[\d,]+", hp_sub.get_text())
        if nums:
            return int(nums[0].replace(",", ""))
    hp_text = card.select_one(".hp-text")
    if hp_text:
        nums = re.findall(r"[\d,]+", hp_text.get_text())
        if nums:
            return int(nums[0].replace(",", ""))
    return 0


def _extract_your_damage(card: Tag) -> int:
    # Primary: data-userdmg is rendered on every card (joined or not).
    # #yourDamageValue and the "DMG" chip only exist for actively-joined cards,
    # so on re-fetch after meeting the loot threshold they return 0 and the
    # farmer mistakenly re-attacks.
    raw = card.get("data-userdmg")
    if raw:
        try:
            return int(str(raw).replace(",", ""))
        except ValueError:
            pass
    dmg_span = card.select_one("[id='yourDamageValue']")
    if dmg_span:
        nums = re.findall(r"[\d,]+", dmg_span.get_text())
        if nums:
            return int(nums[0].replace(",", ""))
    for chip in card.select(".chip"):
        text_upper = chip.get_text().upper()
        if "DMG" in text_upper or "YOU" in text_upper:
            nums = re.findall(r"[\d,]+", chip.get_text())
            if nums:
                return int(nums[0].replace(",", ""))
            break
    return 0


def _extract_image(card: Tag) -> str:
    img_el = card.select_one("img")
    if img_el:
        src = img_el.get("src", "")
        if src and "1x1" not in src and "spacer" not in src:
            if not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            return src
    for el in card.select("[style]"):
        url_m = re.search(r"url\(['\"]?([^'\")\s]+)['\"]?\)", el.get("style", ""))
        if url_m:
            src = url_m.group(1)
            if not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            return src
    return ""


def _detect_joined(card: Tag) -> bool:
    text = card.get_text().lower()
    return "continue the battle" in text or "continue battle" in text


def _parse_damage_value(data: dict, prev_hp: int | None = None) -> int:
    """Extract damage from server response with multiple fallback strategies."""
    # 1) Direct "damage" field
    if "damage" in data:
        try:
            v = int(str(data["damage"]).replace(",", ""))
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass

    # 2) "totaldmgdealt" field
    if "totaldmgdealt" in data:
        try:
            v = int(str(data["totaldmgdealt"]).replace(",", ""))
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass

    # 3) Parse from message text (strip HTML tags)
    msg = re.sub(r"<[^>]+>", "", data.get("message", ""))
    m = re.search(r"dealt\s+([\d,]+)\s*damage", msg, re.IGNORECASE)
    if not m:
        m = re.search(r"([\d,]+)\s*damage", msg, re.IGNORECASE)
    if m:
        try:
            v = int(m.group(1).replace(",", ""))
            if v > 0:
                return v
        except ValueError:
            pass

    # 4) Calculate from HP change
    if prev_hp and prev_hp > 0 and "hp" in data and isinstance(data["hp"], dict):
        try:
            new_hp = int(data["hp"].get("value", prev_hp))
            diff = prev_hp - new_hp
            if diff > 0:
                return diff
        except (ValueError, TypeError):
            pass

    return 0


# ── EXP / Loot parsing ───────────────────────────────────────────────────────


def parse_player_stats(html: str) -> PlayerStats:
    """Parse player stats (EXP, level, stamina) from the game top bar.

    Actual HTML structure:
      <div class="gtb-exp-top"><span>EXP&nbsp;</span><span>450,694 / 739,700</span></div>
      <div class="gtb-level">LV 343</div>
      <span id="stamina_span">0</span> / 1,540
    """
    stats = PlayerStats()
    soup = BeautifulSoup(html, "html.parser")

    # EXP: inside .gtb-exp-top, second span has "450,694 / 739,700"
    exp_top = soup.select_one(".gtb-exp-top")
    if exp_top:
        text = exp_top.get_text()
        m = re.search(r"([\d,]+)\s*/\s*([\d,]+)", text)
        if m:
            stats.exp_current = int(m.group(1).replace(",", ""))
            stats.exp_max = int(m.group(2).replace(",", ""))

    # Level: inside .gtb-level, "LV 343"
    lv_el = soup.select_one(".gtb-level")
    if lv_el:
        m = re.search(r"(\d+)", lv_el.get_text())
        if m:
            stats.level = int(m.group(1))

    # Stamina: <span id="stamina_span">0</span> / 1,540
    # The span is inside a .gtb-value which contains "0 / 1,540"
    stam_span = soup.select_one("#stamina_span")
    if stam_span:
        try:
            stats.stamina_current = int(stam_span.get_text().replace(",", ""))
        except ValueError:
            pass
        # Max stamina: look at the .gtb-value parent that contains "X / Y"
        gtb_value = stam_span.find_parent(class_="gtb-value")
        if gtb_value:
            m = re.search(r"/\s*([\d,]+)", gtb_value.get_text())
            if m:
                stats.stamina_max = int(m.group(1).replace(",", ""))
        else:
            # Fallback: get the next sibling text after the span
            next_text = stam_span.next_sibling
            if next_text and isinstance(next_text, str):
                m = re.search(r"/\s*([\d,]+)", next_text)
                if m:
                    stats.stamina_max = int(m.group(1).replace(",", ""))

    return stats


def parse_dead_monsters(html: str) -> list[DeadMonster]:
    """Parse dead monster cards from a wave page.

    Dead cards use data-* attributes:
      <div class="monster-card" data-monster-id="39850860" data-dead="1"
           data-name="lizardman shadowclaw" data-userdmg="3576694" ...>
    """
    soup = BeautifulSoup(html, "html.parser")
    dead: list[DeadMonster] = []

    for card in soup.select('.monster-card[data-dead="1"]'):
        mid = card.get("data-monster-id", "")
        if not mid:
            continue

        name = card.get("data-name", "").title()
        try:
            your_dmg = int(card.get("data-userdmg", "0"))
        except ValueError:
            your_dmg = 0

        dead.append(DeadMonster(id=mid, name=name, your_dmg=your_dmg))

    return dead


def parse_exp_per_dmg(html: str) -> float:
    """Parse EXP/DMG ratio from a battle page.

    Expected pattern: EXP / DMG  0.007000
    """
    m = re.search(r"EXP\s*/\s*DMG[\s\S]*?([\d.]+)", html)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 0.0


def parse_unclaimed_kills(html: str) -> int:
    """Parse 'Unclaimed kills: 44' count from wave page."""
    m = re.search(r"Unclaimed\s+kills[:\s]*(\d+)", html)
    if m:
        return int(m.group(1))
    return 0


# ── Reaction / Stamina farming parsing ──────────────────────────────────────


def parse_farmed_today(html: str) -> tuple[int, int]:
    """Parse 'Farmed today X / Y' from user info stamina pills.

    HTML:
      <span class="stamina-pill">🌾 Farmed today
        <span class="val">0 / 1,000</span>
      </span>

    Returns (farmed, cap) e.g. (0, 1000).
    """
    soup = BeautifulSoup(html, "html.parser")
    for pill in soup.select(".stamina-pill"):
        if "Farmed today" in pill.get_text():
            val = pill.select_one(".val")
            if val:
                m = re.search(r"([\d,]+)\s*/\s*([\d,]+)", val.get_text())
                if m:
                    farmed = int(m.group(1).replace(",", ""))
                    cap = int(m.group(2).replace(",", ""))
                    return farmed, cap
    return 0, 1000


def parse_chapter_list(html: str) -> list[tuple[str, str]]:
    """Extract all (manga_id, chapter) pairs from a chapter page.

    The chapter page has a <select> dropdown like:
      <option value="/chaptered.php?manga=12680&chapter=1">Chapter:1</option>
      <option value="/chaptered.php?manga=12680&chapter=63" selected>Chapter:63</option>

    Returns list of (manga_id, chapter_number) tuples, e.g. [("12680", "1"), ...].
    """
    chapters: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for m in re.finditer(
        r'/chaptered\.php\?manga=(\d+)&(?:amp;)?chapter=([^"&\s\']+)', html
    ):
        pair = (m.group(1), m.group(2))
        if pair not in seen:
            seen.add(pair)
            chapters.append(pair)

    return chapters


def parse_manga_links(html: str) -> list[str]:
    """Extract manga series links from a listing page (lastupdates, homepage, etc.).

    Looks for <a href="/manga/Title-Name"> or <a href="/title/Title-Name/...">.
    Returns deduplicated list of absolute manga page URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()

    for a in soup.select("a[href]"):
        href = str(a.get("href", ""))
        if not href:
            continue

        # Match manga series pages
        if "/manga/" in href or "/title/" in href:
            # Normalize — keep only the manga base URL
            if not href.startswith("http"):
                href = f"{BASE_URL}/{href.lstrip('/')}"
            if href not in seen:
                seen.add(href)
                links.append(href)

        # Also match direct chapter links on listing pages
        if "chaptered.php" in href and "manga=" in href:
            if not href.startswith("http"):
                href = f"{BASE_URL}/{href.lstrip('/')}"
            if href not in seen:
                seen.add(href)
                links.append(href)

    return links


def _parse_stamina_value(desc: str, name: str) -> int:
    """Return stamina restored per use, or 0 for full-refill potions.

    "Refills 20 Stamina" → 20, "Refills 5000 Stamina(Or 50% max stamina)" → 5000,
    "Fully Refills Your Stamina" → 0.
    """
    sv = re.search(r"Refills?\s+(\d+)\s*Stamina", desc, re.IGNORECASE)
    if sv:
        return int(sv.group(1))
    if re.search(r"full(?:y)?\s+refills?", desc, re.IGNORECASE):
        return 0
    # Name-based fallback — Full Stamina Potion is a full refill
    if re.search(r"\bfull\b", name, re.IGNORECASE):
        return 0
    return 0


def parse_stamina_potions(html: str) -> list[StaminaPotion]:
    """Parse stamina potions from the inventory / battle drawer page.

    Supports the current `.potion-card` layout (data-inv-id, data-item-id,
    .potion-name, .potion-desc, .potion-qty-left) and falls back to the
    legacy `.slot-box` + `useItem(...)` onclick format.

    Returns potions sorted: small partial potions first, large partial next,
    full-refill potions last.
    """
    soup = BeautifulSoup(html, "html.parser")
    potions: list[StaminaPotion] = []
    seen_inv_ids: set[str] = set()

    # Current layout: .potion-card with data attributes
    for card in soup.select(".potion-card"):
        inv_id = card.get("data-inv-id", "")
        item_id = card.get("data-item-id", "")
        if not inv_id or not item_id:
            continue

        name_el = card.select_one(".potion-name span")
        img = card.select_one("img")
        name = (name_el.get_text(strip=True) if name_el else "") or (img.get("alt", "") if img else "")
        if "stamina" not in name.lower():
            continue

        qty_el = card.select_one(".potion-qty-left")
        try:
            qty = int(qty_el.get_text(strip=True)) if qty_el else 0
        except ValueError:
            qty = 0
        if qty <= 0:
            # Fall back to the Use button's data-max attribute
            btn = card.select_one(".potion-use-btn, button[data-max]")
            if btn:
                try:
                    qty = int(btn.get("data-max", "0"))
                except ValueError:
                    qty = 0
        if qty <= 0:
            continue

        desc_el = card.select_one(".potion-desc")
        desc = desc_el.get_text(strip=True) if desc_el else ""

        potions.append(StaminaPotion(
            inv_id=inv_id,
            item_type=item_id,
            name=name,
            quantity=qty,
            desc=desc,
            stamina_value=_parse_stamina_value(desc, name),
        ))
        seen_inv_ids.add(inv_id)

    # Legacy layout: .slot-box with useItem(...) onclick
    for box in soup.select(".slot-box"):
        info_btn = box.select_one(".info-btn")
        if not info_btn:
            continue

        name = info_btn.get("data-name", "")
        desc = info_btn.get("data-desc", "")
        if "stamina" not in name.lower():
            continue

        btn = box.select_one("button.btn[onclick*='useItem']")
        if not btn:
            continue

        onclick = btn.get("onclick", "")
        m = re.search(r"useItem\(\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'\s*,\s*(\d+)", onclick)
        if not m:
            continue

        inv_id = m.group(1)
        if inv_id in seen_inv_ids:
            continue
        qty = int(m.group(4))
        if qty <= 0:
            continue

        potions.append(StaminaPotion(
            inv_id=inv_id,
            item_type=m.group(2),
            name=name,
            quantity=qty,
            desc=desc,
            stamina_value=_parse_stamina_value(desc, name),
        ))
        seen_inv_ids.add(inv_id)

    # Small partial → large partial → full-refill last
    potions.sort(key=lambda p: (1, 0) if p.is_full else (0, p.stamina_value))
    return potions


def parse_pvp_solo_tokens(html: str) -> int:
    """Parse solo PvP tokens from the pvp.php page.

    HTML structure:
      <div class="info-pill"><strong>Tokens:</strong> <span>23</span></div>
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: find .info-pill containing "Tokens" and read the span value
    for pill in soup.select(".info-pill"):
        text = pill.get_text()
        if "Tokens" in text:
            span = pill.select_one("span")
            if span:
                try:
                    return int(span.get_text().strip().replace(",", ""))
                except ValueError:
                    pass
            # Fallback: parse from combined text
            m = re.search(r"Tokens[:\s]*([\d,]+)", text, re.IGNORECASE)
            if m:
                return int(m.group(1).replace(",", ""))

    # Strategy 2: broader regex on raw HTML
    m = re.search(r"Tokens[:\s]*</strong>\s*<span>([\d,]+)</span>", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    # Strategy 3: plain text fallback
    m = re.search(r"Tokens[:\s]*([\d,]+)", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    return 0


def parse_pvp_party_status(html: str) -> dict:
    """Parse the team/party block from pvp.php.

    Returns:
      {
        "in_party": bool,           # user belongs to a party
        "is_leader": bool,          # user can start party matches / disband
        "tokens": int,              # current party tokens
        "tokens_max": int,          # cap (e.g. 10)
        "party_name": str,
      }

    Detection strategy (best-effort, robust to markup tweaks):
      • "Disband Party" button → user is leader
      • "Find Party Match" button → user is in a party
      • Tokens cell shows "9 / 10" within the party section
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    lower = text.lower()

    is_leader = "disband party" in lower
    in_party = "find party match" in lower or is_leader

    tokens = 0
    tokens_max = 0

    # Tokens: 9 / 10  (allow optional whitespace, optional thousands separators)
    m = re.search(
        r"Tokens[^A-Za-z0-9]*([\d,]+)\s*/\s*([\d,]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            tokens = int(m.group(1).replace(",", ""))
            tokens_max = int(m.group(2).replace(",", ""))
        except ValueError:
            pass

    party_name = ""
    # Try to grab the heading just above the party stats — fall back gracefully.
    for h in soup.select("h1, h2, h3, .party-name, .team-name, [data-party-name]"):
        h_text = h.get_text(strip=True)
        if not h_text:
            continue
        if "party" in h_text.lower() and len(h_text) < 60:
            party_name = h_text
            break

    return {
        "in_party": in_party,
        "is_leader": is_leader,
        "tokens": tokens,
        "tokens_max": tokens_max,
        "party_name": party_name,
    }


def parse_character_stats(html: str) -> CharacterStats:
    """Parse unspent points and current stat values from stats.php.

    Expected HTML structure (Current Stats card):
      <div class="row"><span>ATTACK</span><span>200</span></div>
      <div class="row"><span>DEFENSE</span><span>200</span></div>
      <div class="row"><span>STAMINA</span><span>1590</span></div>

    And unspent points:
      <div class="row"><span>Unspent Points</span><span>5</span></div>
    """
    stats = CharacterStats()

    # Unspent points
    m = re.search(r"Unspent\s+Points\s*</span>\s*<span[^>]*>\s*(\d+)", html, re.IGNORECASE)
    if not m:
        m = re.search(r"Unspent\s+Points.*?(\d+)", html, re.IGNORECASE | re.DOTALL)
    if m:
        stats.unspent = int(m.group(1))

    # Current stat values — look in the "Current Stats" card
    for stat_name in ("attack", "defense", "stamina"):
        pattern = rf"{stat_name}\s*</span>\s*<span[^>]*>\s*([\d,]+)"
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            val = int(m.group(1).replace(",", ""))
            setattr(stats, stat_name, val)

    return stats


def parse_monster_loot(html: str) -> tuple[str, list[LootItem]]:
    """Parse the 'Possible Loot' section from a battle.php page.

    Real HTML structure:
      <div class="panel">
        <strong>🎁 Possible Loot</strong>
        <div class="loot-grid">
          <h4 class="tier-head tier-LEGENDARY">LEGENDARY</h4>
          <div class="loot-row">
            <div class="loot-card locked|unlocked">
              <div class="loot-img-wrap">
                <img alt="..." src="images/items/..."/>
                <div class="lock-badge">Locked</div>
              </div>
              <div class="loot-meta">
                <div class="loot-name">Goblin Essence</div>
                <div class="loot-desc">A swirling green vial...</div>
                <div class="loot-stats">
                  <span class="chip">Drop: 6%</span>
                  <span class="chip">DMG req: 70,000</span>
                  <span class="chip tierchip legendary">LEGENDARY</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

    Returns (monster_name, list_of_loot_items).
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[LootItem] = []

    # Extract monster name from .card-title
    monster_name = ""
    card_title = soup.select_one(".card-title")
    if card_title:
        # Strip leading emoji: "🧟 Goblin Skirmisher" → "Goblin Skirmisher"
        monster_name = re.sub(r"^[^\w]+", "", card_title.get_text(strip=True)).strip()
        # Remove any trailing chip text (stop at first tag boundary)
        # card-title may contain child <span> chips — only take direct text
        direct_texts = [t for t in card_title.strings]
        if direct_texts:
            monster_name = re.sub(r"^[^\w]+", "", direct_texts[0].strip()).strip()

    # Find loot-grid container
    loot_grid = soup.select_one(".loot-grid")
    if not loot_grid:
        return monster_name, items

    # Parse each .loot-card
    for card in loot_grid.select(".loot-card"):
        # Name
        name_el = card.select_one(".loot-name")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        # Description
        desc_el = card.select_one(".loot-desc")
        desc = desc_el.get_text(strip=True) if desc_el else ""

        # Image
        image = ""
        img_el = card.select_one(".loot-img-wrap img")
        if img_el:
            src = img_el.get("src", "")
            if src:
                if not src.startswith("http"):
                    src = f"{BASE_URL}/{src.lstrip('/')}"
                image = src

        # Parse chips for drop rate, DMG req, rarity
        drop_rate = ""
        dmg_required = 0
        rarity = ""

        for chip in card.select(".chip"):
            chip_text = chip.get_text(strip=True)

            dm = re.match(r"Drop:\s*([\d.]+%?)", chip_text)
            if dm:
                drop_rate = dm.group(1)
                if "%" not in drop_rate:
                    drop_rate += "%"
                continue

            dmg_m = re.match(r"DMG\s*req:\s*([\d,]+)", chip_text)
            if dmg_m:
                dmg_required = int(dmg_m.group(1).replace(",", ""))
                continue

            # Rarity chip (has class "tierchip")
            classes = chip.get("class", [])
            if "tierchip" in classes:
                rarity = chip_text.upper()

        items.append(LootItem(
            name=name,
            description=desc,
            image=image,
            drop_rate=drop_rate,
            dmg_required=dmg_required,
            rarity=rarity,
        ))

    return monster_name, items


def parse_collection_progress(html: str, collection_id: int) -> dict | None:
    """Parse a single collection card from /collections.php.

    Returns:
      {
        "id": int, "name": str, "reward": str,
        "items": [{"name": str, "need": int, "have": int, "image": str}, ...]
      }
    or None if the card isn't found.
    """
    soup = BeautifulSoup(html, "html.parser")
    card = soup.select_one(f'.card[data-col-id="{collection_id}"]')
    if not card:
        return None

    title_el = card.select_one(".title")
    name = title_el.get_text(strip=True) if title_el else ""
    reward = ""
    reward_el = card.select_one(".reward")
    if reward_el:
        reward = reward_el.get_text(" ", strip=True).replace("Reward:", "").strip()

    items: list[dict] = []
    for req in card.select(".req-item"):
        nm_el = req.select_one("div[style*='font-weight:600']") or req.select_one("div > div")
        if not nm_el:
            continue
        item_name = nm_el.get_text(strip=True)
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
        img = req.select_one("img.req-img")
        src = img.get("src", "") if img else ""
        if src and not src.startswith("http"):
            src = f"{BASE_URL}/{src.lstrip('/')}"
        items.append({"name": item_name, "need": need, "have": have, "image": src})

    return {"id": collection_id, "name": name, "reward": reward, "items": items}


def _singularize_monster(plural: str) -> str:
    """Best-effort English singular for the monster names used in achievements.

    Covers the patterns that appear in the game:
      Wolves → Wolf, Lynxes → Lynx, Hyenas → Hyena, Bears → Bear,
      Boars → Boar, Crows → Crow, Vipers → Viper, Runestags → Runestag.
    """
    name = plural.strip()
    if not name:
        return name
    low = name.lower()
    if low.endswith("wolves"):
        return name[:-6] + ("Wolf" if name[-6].isupper() else "wolf")
    if low.endswith("ves"):
        return name[:-3] + ("f" if name[-3].islower() else "F")
    if low.endswith("xes") or low.endswith("ses") or low.endswith("zes"):
        return name[:-2]
    if low.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if low.endswith("s") and not low.endswith("ss"):
        return name[:-1]
    return name


def parse_achievements(html: str) -> list[dict]:
    """Parse damage-per-mob achievements from /achievements.php.

    Matches the description pattern "Deal at least N damage to M <MonsterNames>."
    and extracts the progress `X / M` from the same card.

    Returns a list of dicts (page order preserved):
      {title, monster, monster_plural, damage_required, kills_required,
       kills_current, percent}
    """
    soup = BeautifulSoup(html, "html.parser")
    # Collapse the page to line-separated text — resilient to any card layout.
    text = soup.get_text("\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    desc_re = re.compile(
        r"Deal at least\s+([\d,]+)\s+damage to\s+([\d,]+)\s+(.+?)\.?$",
        re.IGNORECASE,
    )
    prog_re = re.compile(r"^([\d,]+)\s*/\s*([\d,]+)$")

    achievements: list[dict] = []
    for i, line in enumerate(lines):
        m = desc_re.match(line)
        if not m:
            continue

        damage_req = int(m.group(1).replace(",", ""))
        kills_req = int(m.group(2).replace(",", ""))
        monster_plural = m.group(3).strip().rstrip(".")

        # Title: nearest preceding non-numeric, non-"rewards" line
        title = ""
        for j in range(i - 1, max(-1, i - 4), -1):
            candidate = lines[j]
            if (not prog_re.match(candidate)
                    and not candidate.lower().startswith("rewards")
                    and candidate.lower() != "claim"):
                title = candidate
                break

        # Progress: first "X / kills_req" line within a small window after desc
        current = 0
        for j in range(i + 1, min(len(lines), i + 6)):
            pm = prog_re.match(lines[j])
            if pm:
                c = int(pm.group(1).replace(",", ""))
                n = int(pm.group(2).replace(",", ""))
                if n == kills_req:
                    current = c
                    break

        monster_singular = _singularize_monster(monster_plural)
        percent = min(100, round(current * 100 / kills_req)) if kills_req else 0

        achievements.append({
            "title": title,
            "monster": monster_singular,
            "monster_plural": monster_plural,
            "damage_required": damage_req,
            "kills_required": kills_req,
            "kills_current": current,
            "percent": percent,
        })

    return achievements


def parse_chapter_id(html: str) -> str | None:
    """Extract the internal chapterid from a chapter page.

    The actual pattern in the page JS is:
      formData.append('chapterid', '630695');

    This is NOT the chapter number — it's an internal DB ID needed for reactions.
    """
    # Strategy 1: formData.append('chapterid', '630695')  — the real pattern
    m = re.search(r"['\"]chapterid['\"]\s*,\s*['\"](\d+)['\"]", html)
    if m:
        return m.group(1)

    # Strategy 2: JS variable assignment
    m = re.search(r"(?:const|let|var)\s+chapter[Ii]d\s*=\s*['\"]?(\d+)", html)
    if m:
        return m.group(1)

    # Strategy 3: key=value style (chapterid = 630558 or chapterid: 630558)
    m = re.search(r"chapterid['\"]?\s*[:=]\s*['\"]?(\d+)", html, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


# ── Class skill detection ────────────────────────────────────────────────────


def parse_class_skills(html: str) -> list[dict]:
    """Parse class skills from a battle page (must have joined the battle).

    Returns list of dicts: [{"id": "8", "name": "Heal", "mp_cost": 20}, ...]
    Empty list means no class / no skills.
    """
    soup = BeautifulSoup(html, "html.parser")
    bar = soup.select_one(".class-skill-bar")
    if not bar:
        return []

    skills = []
    for btn in bar.select(".skill-slot.attack-btn"):
        skill_id = btn.get("data-skill-id", "")
        name = btn.get("data-skill-name", "")
        cost_el = btn.select_one(".skill-cost")
        mp_cost = 0
        if cost_el:
            m = re.search(r"(\d+)\s*MP", cost_el.get_text())
            if m:
                mp_cost = int(m.group(1))
        if skill_id:
            skills.append({"id": skill_id, "name": name, "mp_cost": mp_cost})

    return skills


# ── Quest board parsing ──────────────────────────────────────────────────────


def parse_quest_objective(text: str) -> QuestObjective:
    """Parse an objective string into a QuestObjective.

    Known formats:
      "Kill 5 monster(s) · Monster: Lizardman Shadowclaw · min 3m dmg"
      "Gather 2x item(s) · Item: Goblin Essence"
      "Use 20 skills against monsters"
    """
    text = text.strip()

    # Kill quest
    m = re.match(
        r"Kill\s+(\d+)\s+monster\(s\)\s*·\s*Monster:\s*(.+?)(?:\s*·\s*min\s+([\d.]+)([mk])?\s*dmg)?\s*$",
        text, re.IGNORECASE,
    )
    if m:
        count = int(m.group(1))
        name = m.group(2).strip()
        raw_dmg = float(m.group(3)) if m.group(3) else 0
        suffix = (m.group(4) or "").lower()
        if suffix == "m":
            min_dmg = int(raw_dmg * 1_000_000)
        elif suffix == "k":
            min_dmg = int(raw_dmg * 1_000)
        else:
            min_dmg = int(raw_dmg)
        return QuestObjective(QuestType.KILL, count, name, min_dmg)

    # Gather quest
    m = re.match(
        r"Gather\s+(\d+)x?\s+item\(s\)\s*·\s*Item:\s*(.+)",
        text, re.IGNORECASE,
    )
    if m:
        return QuestObjective(QuestType.GATHER, int(m.group(1)), m.group(2).strip())

    # Skill quest
    m = re.match(r"Use\s+(\d+)\s+skills?\s+against", text, re.IGNORECASE)
    if m:
        return QuestObjective(QuestType.SKILL, int(m.group(1)))

    return QuestObjective(QuestType.UNKNOWN)


def _parse_quest_number(s: str) -> int:
    """Parse a comma-formatted number string like '10,000' -> 10000."""
    return int(s.replace(",", "").strip())


def parse_quest_board(html: str) -> list[Quest]:
    """Parse all quest cards from the adventurers guild page."""
    soup = BeautifulSoup(html, "html.parser")
    quests: list[Quest] = []

    for row in soup.select(".quest-row"):
        # Title
        title_el = row.select_one(".quest-main-title")
        title = title_el.get_text(strip=True) if title_el else ""

        # Description
        desc_el = row.select_one(".quest-main-desc")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # Rank
        tag_el = row.select_one(".quest-tag")
        rank = tag_el.get_text(strip=True) if tag_el else ""
        if rank.lower().startswith("rank:"):
            rank = rank[5:].strip()

        # Objective (structured, from .quest-req-text)
        req_el = row.select_one(".quest-req-text")
        if req_el:
            objective = parse_quest_objective(req_el.get_text(strip=True))
        else:
            # Skill quests have no .quest-requirements — try parsing from description
            objective = parse_quest_objective(description)

        # Reward: "100 AP • 10,000 Gold • 0"
        reward_ap = 0
        reward_gold = 0
        reward_el = row.select_one(".quest-reward")
        if reward_el:
            reward_text = reward_el.get_text(strip=True)
            ap_m = re.search(r"([\d,]+)\s*AP", reward_text)
            gold_m = re.search(r"([\d,]+)\s*Gold", reward_text)
            if ap_m:
                reward_ap = _parse_quest_number(ap_m.group(1))
            if gold_m:
                reward_gold = _parse_quest_number(gold_m.group(1))

        # Status: check for accept button vs cooldown timer vs active
        accept_btn = row.select_one(".quest-accept-btn")
        cooldown_el = row.select_one(".quest-cooldown-timer")
        progress_el = row.select_one(".quest-progress")
        finish_btn = row.select_one(".quest-finish-btn")

        quest_id = 0
        status = QuestStatus.AVAILABLE
        cooldown_ts = 0

        if finish_btn or progress_el:
            status = QuestStatus.ACTIVE
            onclick = (finish_btn or row.select_one(".quest-giveup-btn") or {}).get("onclick", "")
            id_m = re.search(r"(?:finishQuest|giveUpQuest)\((\d+)", onclick)
            if id_m:
                quest_id = int(id_m.group(1))
        elif accept_btn:
            status = QuestStatus.AVAILABLE
            onclick = accept_btn.get("onclick", "")
            id_m = re.search(r"acceptQuest\((\d+)", onclick)
            if id_m:
                quest_id = int(id_m.group(1))
        elif cooldown_el:
            status = QuestStatus.COOLDOWN
            cooldown_ts = int(cooldown_el.get("data-cooldown-ts", "0"))

        quests.append(Quest(
            title=title,
            quest_id=quest_id,
            description=description,
            rank=rank,
            reward_ap=reward_ap,
            reward_gold=reward_gold,
            objective=objective,
            status=status,
            cooldown_ts=cooldown_ts,
        ))

    return quests


def parse_active_quest(html: str) -> ActiveQuest | None:
    """Parse the currently active quest with progress from the guild page.

    Active quests show a .quest-progress element with text like "3 / 5" and
    optionally a .quest-finish-btn when completed.
    """
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.select(".quest-row"):
        progress_el = row.select_one(".quest-progress")
        finish_btn = row.select_one(".quest-finish-btn")
        giveup_btn = row.select_one(".quest-giveup-btn")

        if not progress_el and not finish_btn and not giveup_btn:
            continue

        # This is the active quest
        title_el = row.select_one(".quest-main-title")
        title = title_el.get_text(strip=True) if title_el else ""

        req_el = row.select_one(".quest-req-text")
        desc_el = row.select_one(".quest-main-desc")
        if req_el:
            objective = parse_quest_objective(req_el.get_text(strip=True))
        elif desc_el:
            objective = parse_quest_objective(desc_el.get_text(strip=True))
        else:
            objective = QuestObjective()

        quest_id = 0
        for btn in [finish_btn, giveup_btn]:
            if btn:
                onclick = btn.get("onclick", "")
                id_m = re.search(r"\((\d+)", onclick)
                if id_m:
                    quest_id = int(id_m.group(1))
                    break

        quest = Quest(
            title=title,
            quest_id=quest_id,
            objective=objective,
            status=QuestStatus.ACTIVE,
        )

        # Parse progress: "3 / 5" or similar
        progress = 0
        target_count = objective.target_count
        if progress_el:
            prog_text = progress_el.get_text(strip=True)
            prog_m = re.search(r"(\d+)\s*/\s*(\d+)", prog_text)
            if prog_m:
                progress = int(prog_m.group(1))
                target_count = int(prog_m.group(2))

        completed = finish_btn is not None and not finish_btn.get("disabled")

        return ActiveQuest(
            quest=quest,
            progress=progress,
            target_count=target_count,
            completed=completed,
        )

    return None
