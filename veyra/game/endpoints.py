BASE_URL = "https://demonicscans.org"

LOGIN_URL = f"{BASE_URL}/signin.php"
JOIN_URL = f"{BASE_URL}/user_join_battle.php"
DAMAGE_URL = f"{BASE_URL}/damage.php"
LOOT_URL = f"{BASE_URL}/loot.php"
BATTLE_URL = f"{BASE_URL}/battle.php"

# PvP
PVP_URL = f"{BASE_URL}/pvp.php"
PVP_MATCHMAKE_URL = f"{BASE_URL}/pvp_matchmake.php"
PVP_BATTLE_ACTION_URL = f"{BASE_URL}/pvp_battle_action.php"
PVP_BATTLE_STATE_URL = f"{BASE_URL}/pvp_battle_state.php"
PVP_BATTLE_URL = f"{BASE_URL}/pvp_battle.php"

# Manga / reaction stamina farming
REACT_URL = f"{BASE_URL}/postreaction.php"
CHAPTER_URL = f"{BASE_URL}/chaptered.php"
LAST_UPDATES_URL = f"{BASE_URL}/lastupdates.php"
INVENTORY_URL = f"{BASE_URL}/inventory.php"
USE_ITEM_URL = f"{BASE_URL}/use_item.php"
STATS_URL = f"{BASE_URL}/stats.php"
ALLOCATE_STAT_URL = f"{BASE_URL}/stats_ajax.php"

STAMINA_PER_REACTION = 2
FARMED_DAILY_CAP = 1000

WAVE_MAP: dict[int, str] = {
    1: f"{BASE_URL}/active_wave.php?gate=3&wave=3",
    2: f"{BASE_URL}/active_wave.php?gate=3&wave=5",
    3: f"{BASE_URL}/active_wave.php?gate=3&wave=8",
}

STAMINA_OPTIONS = [
    {"label": "1 Stamina", "cost": 1, "skill_id": "0"},
    {"label": "10 Stamina", "cost": 10, "skill_id": "-1"},
    {"label": "50 Stamina", "cost": 50, "skill_id": "-2"},
    {"label": "100 Stamina", "cost": 100, "skill_id": "-3"},
    {"label": "200 Stamina", "cost": 200, "skill_id": "-4"},
]

# Step-down mapping: when current stamina cost fails, try the next lower
STAMINA_STEP_DOWN: dict[int, int] = {
    200: 100,
    100: 50,
    50: 10,
    10: 1,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

ATTACK_EXTRA_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "*/*",
}

# Monster classes for filtering
MONSTER_CLASSES: dict[str, list[str]] = {
    "goblin": ["goblin", "hobgoblin"],
    "orc": ["orc", "troll"],
    "lizardman": ["lizardman"],
}


def get_stamina_option(label: str) -> dict:
    for opt in STAMINA_OPTIONS:
        if opt["label"] == label:
            return opt
    return STAMINA_OPTIONS[1]  # default 10 stamina


def step_down_stamina(current_cost: int) -> dict | None:
    next_cost = STAMINA_STEP_DOWN.get(current_cost)
    if next_cost is None:
        return None
    for opt in STAMINA_OPTIONS:
        if opt["cost"] == next_cost:
            return opt
    return None
