"""HTML/JSON parsing — ported from slasher_app.py:117-286."""

import re

from bs4 import BeautifulSoup, Tag

from veyra.game.endpoints import BASE_URL
from veyra.game.types import AttackResult, CharacterStats, DeadMonster, Monster, MonsterGroup, PlayerStats, StaminaPotion


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
    dmg_span = card.select_one("[id='yourDamageValue']")
    if dmg_span:
        nums = re.findall(r"[\d,]+", dmg_span.get_text())
        if nums:
            return int(nums[0].replace(",", ""))
    for chip in card.select(".chip"):
        if "DMG" in chip.get_text().upper():
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


def parse_stamina_potions(html: str) -> list[StaminaPotion]:
    """Parse stamina potions from the inventory page.

    Each slot has an info button with data-name/data-desc and a Use button with:
        useItem(inv_id, item_type_id, 'Name', quantity)

    Returns potions sorted: small potions first (to conserve full potions).
    """
    soup = BeautifulSoup(html, "html.parser")
    potions: list[StaminaPotion] = []

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

        qty = int(m.group(4))
        if qty <= 0:
            continue

        # Parse stamina restored: "Refills 20 Stamina" → 20, "Fully Refills" → 0
        stam_val = 0
        sv = re.search(r"Refills?\s+(\d+)\s+Stamina", desc, re.IGNORECASE)
        if sv:
            stam_val = int(sv.group(1))

        potions.append(StaminaPotion(
            inv_id=m.group(1),
            item_type=m.group(2),
            name=name,
            quantity=qty,
            desc=desc,
            stamina_value=stam_val,
        ))

    # Small potions first, full potions last (conserve the more valuable ones)
    potions.sort(key=lambda p: 1 if p.is_full else 0)
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
